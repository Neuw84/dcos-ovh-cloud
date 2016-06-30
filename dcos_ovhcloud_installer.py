#!/usr/bin/env python3
import ovh
import time
import requests
import os.path
import stat
import yaml
import sys
import subprocess
import atexit
import logging
import argparse
import socket
from retrying import retry

log_level = logging.DEBUG
logging.basicConfig(level=logging.WARN, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('__main__').setLevel(log_level)
logging.getLogger('DCOSInstall').setLevel(log_level)
logging.getLogger('OVHInstances').setLevel(log_level)
log = logging.getLogger(__name__)


def main(argv):
    p = argparse.ArgumentParser(description='Install DC/OS on OVH Cloud')
    p.add_argument('--url',      help='URL to dcos_generate_config.sh',
                   default='https://downloads.dcos.io/dcos/EarlyAccess/dcos_generate_config.sh')
    p.add_argument('--project',  help='OVH Cloud Project Name', required=True)
    p.add_argument('--flavor',   help='OVH Cloud Machine Type (default hg-15)', default='hg-15')
    p.add_argument('--image',    help='OVH Cloud OS Image (default Centos 7)', default='Centos 7')
    p.add_argument('--ssh-key',  help='OVH Cloud SSH Key Name', required=True)
    p.add_argument('--ssh-user', help='SSH Username (default centos)', default='centos')
    p.add_argument('--region',   help='OVH Cloud Region (default SBG1)', default='SBG1')
    p.add_argument('--name',     help='OVH Cloud VM Instance Name(s)', default='Test')
    p.add_argument('--masters',  help='Number of Master Instances', default=1, type=int)
    p.add_argument('--agents',   help='Number of Agent Instances', default=1, type=int)
    args = p.parse_args(argv)

    dcos = DCOSInstall(args, OVHInstances(args))
    dcos.deploy()

    input('Press Enter to DESTROY all instances...')
    sys.exit(0)


class DCOSInstall:
    def __init__(self, args, oi):
        self.log = logging.getLogger(self.__class__.__name__)
        self.args = args
        self.oi = oi
        self.masters = []
        self.agents = []
        self.installer = 'dcos_generate_config.sh'
        self.dcos_config = {
            'bootstrap_url': 'file:///opt/dcos_install_tmp',
            'cluster_name': 'OVH Test',
            'exhibitor_storage_backend': 'static',
            'master_discovery': 'static',
            'process_timeout': 10000,
            'resolvers': ['8.8.8.8', '8.8.4.4'],
            'ssh_port': 22,
            'telemetry_enabled': 'false'
        }

    def deploy(self):
        self.download()
        self.oi.create_instances()
        self.write_config()
        self.system_prep()
        self.install()

    def download(self):
        dcos_url = self.args.url
        self.log.info('Downloading DC/OS Installer from {}'.format(dcos_url))
        r = requests.get(dcos_url, stream=True)
        remote_size = int(r.headers.get('content-length'))
        store = True
        if os.path.isfile(self.installer):
            local_size = os.path.getsize(self.installer)
            if local_size == remote_size:
                self.log.info(
                    'Local file {} matches remote file size {} - skipping download'.format(self.installer, remote_size))
                store = False
            else:
                self.log.info(
                    "Local file {} with size {} doesn't match remote file size {}".format(self.installer, local_size,
                                                                                          remote_size))

        if store:
            chunk_size = 1024
            downloaded = 0
            last_per = -1
            with open(self.installer, 'wb') as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += chunk_size
                        per = int(downloaded * 100 / remote_size)
                        if per != last_per and per % 10 == 0:
                            self.log.debug('{}%'.format(per))
                        last_per = per
                f.flush()
            os.chmod(self.installer, os.stat(self.installer).st_mode | stat.S_IEXEC)

        if not os.path.isfile('genconf/ip-detect'):
            self.log.error('genconf/ip-detect is missing'
                           ' (details: https://dcos.io/docs/1.7/administration/installing/custom/advanced/)')
            sys.exit(1)
        if not os.path.isfile('genconf/ssh_key'):
            self.log.error('genconf/ssh_key is missing (private key to ssh into nodes)')
            sys.exit(1)

    def stream_cmd(self, cmd):
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
        while p.poll() is None:
            sys.stdout.write(p.stdout.readline().decode(sys.stdout.encoding))
        if p.returncode != 0:
            msg = 'Command {} returned code {}'.format(cmd, p.returncode)
            self.log.error(msg)
            raise ValueError(msg)
        return True

    def system_prep(self):
        self.log.info('Preparing OVH systems for DC/OS installation')
        user = self.args.ssh_user
        remote_cmd = 'sudo systemctl disable firewalld; sudo systemctl stop firewalld'
        for i in self.oi.instances:
            host = i['ip']
            cmd = "ssh -tt -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o UserKnownHostsFile=/dev/null" \
                  " -o BatchMode=yes -i genconf/ssh_key {}@{} '{}' <&-".format(user, host, remote_cmd)
            self.log.debug('Preparing {}'.format(host))
            retries = 3
            success = False
            while retries > 0 and not success:
                try:
                    self.stream_cmd(cmd)
                    success = True
                except ValueError:
                    retries -= 1
                    self.log.debug('Failed to prepare {} - {} retries left'.format(host, retries))
                    time.sleep(10)

    def install(self):
        self.log.info('Running the DC/OS installer')
        try:
            self.stream_cmd('./{} --genconf'.format(self.installer))
            self.stream_cmd('./{} --install-prereqs'.format(self.installer))
            self.stream_cmd('./{} --preflight'.format(self.installer))
            self.stream_cmd('./{} --deploy'.format(self.installer))
            self.stream_cmd('./{} --postflight'.format(self.installer))
        except ValueError:
            self.log.critical('An error occurred while installing DC/OS - aborting')
            sys.exit(1)

        self.log.info('DC/OS is available at the following master endpoints:')
        for master in self.dcos_config['master_list']:
            self.log.info('\thttps://{master}/\tssh://{user}@{master}'.format(master=master, user=self.args.ssh_user))
        self.log.info('The following agents have been installed:')
        for agent in self.dcos_config['agent_list']:
            self.log.info('\tssh://{}@{}'.format(self.args.ssh_user, agent))
        self.log.warn('WARNING - All host firewalls are OPEN! Service ports are publicly available!')

    def write_config(self):
        instances = self.oi.instances
        master = self.args.masters
        agents = self.args.agents
        user = self.args.ssh_user
        self.dcos_config['master_list'] = [i['ip'] for i in instances][:master]
        self.dcos_config['agent_list'] = [i['ip'] for i in instances][-agents:]
        self.dcos_config['ssh_user'] = user
        with open('genconf/config.yaml', 'w') as outfile:
            outfile.write(yaml.dump(self.dcos_config))


class OVHInstances:
    def __init__(self, args):
        self.log = logging.getLogger(self.__class__.__name__)
        atexit.register(self.cleanup)
        self.args = args
        self.instances = []
        self.ovh = OVHClient()
        self._projects = {}
        self._flavors = {}
        self._images = {}
        self._ssh_keys = {}
        self.project_id = self.projects[self.args.project]['project_id']

    @property
    def projects(self):
        if len(self._projects) == 0:
            self.log.debug('Fetching projects from OVH API')
            for serviceName in self.ovh.get('/cloud/project'):
                project = self.ovh.get('/cloud/project/{}'.format(serviceName))
                self.log.debug('Found project {} with id {}'.format(project['description'], project['project_id']))
                self._projects[project['description']] = project
        return self._projects

    @property
    def flavors(self):
        if len(self._flavors) == 0:
            self.log.debug('Fetching machine types from OVH API')
            for flavor in self.ovh.get('/cloud/project/{}/flavor'.format(self.project_id)):
                if flavor['osType'] == 'linux':
                    if flavor['region'] not in self._flavors:
                        self._flavors[flavor['region']] = {}
                    self.log.debug('Found type {} in region {} with id {}'.format(flavor['name'], flavor['region'],
                                                                                  flavor['id']))
                    self._flavors[flavor['region']][flavor['name']] = flavor['id']
        return self._flavors

    @property
    def images(self):
        if len(self._images) == 0:
            self.log.debug('Fetching OS Images from OVH API')
            for image in self.ovh.get('/cloud/project/{}/image'.format(self.project_id)):
                if image['region'] not in self._images:
                    self._images[image['region']] = {}
                self.log.debug(
                    'Found image {} in region {} with id {}'.format(image['name'], image['region'], image['id']))
                self._images[image['region']][image['name']] = image['id']
        return self._images

    @property
    def ssh_keys(self):
        if len(self._ssh_keys) == 0:
            self.log.debug('Fetching ssh keys from OVH API')
            for ssh_key in self.ovh.get('/cloud/project/{}/sshkey'.format(self.project_id)):
                for region in ssh_key['regions']:
                    if region not in self._ssh_keys:
                        self._ssh_keys[region] = {}
                    self.log.debug(
                        'Found ssh key {} in region {} with id {}'.format(ssh_key['name'], region, ssh_key['id']))
                    self._ssh_keys[region][ssh_key['name']] = ssh_key['id']
        return self._ssh_keys

    def cleanup(self):
        self.log.info('Cleaning up instances')
        for instance in self.instances:
            self.cleanup_instance(instance['id'])

    def cleanup_instance(self, instance_id):
        self.log.debug('Removing instance {}'.format(instance_id))
        self.ovh.delete('/cloud/project/{}/instance/{}'.format(self.project_id, instance_id))

    def create_instance(self, name, region, flavor, image, ssh_key, num=1):
        flavor_id = self.flavors[region][flavor]
        image_id = self.images[region][image]
        ssh_key_id = self.ssh_keys[region][ssh_key]
        s = '' if num == 1 else 's'
        self.log.debug('Creating {} instance{} in region {} of type {} with image {}'.format(num, s, region, flavor, image))
        try:
            r = self.ovh.post('/cloud/project/{}/instance/bulk'.format(self.project_id), serviceName=self.project_id,
                              flavorId=flavor_id, imageId=image_id, name=name, region=region, sshKeyId=ssh_key_id,
                              monthlyBilling=False, number=num)
            instances = [{'id': i['id']} for i in r]
        except ovh.exceptions.APIError:
            raise

        return instances

    def recover_instance_error(self, instance_id, name, region, flavor, image, ssh_key):
        self.log.info('Encountered OVH Cloud ERROR - trying to replace failed instance')
        del(self.instances[next(i for (i, d) in enumerate(self.instances) if d["id"] == instance_id)])
        self.cleanup_instance(instance_id)
        self.instances.extend(self.create_instance(name, region, flavor, image, ssh_key))

    def create_instances(self):
        name = self.args.name
        num = self.args.masters + self.args.agents
        region = self.args.region
        flavor = self.args.flavor
        image = self.args.image
        ssh_key = self.args.ssh_key

        self.log.info('Sending instance creation requests')
        self.instances.extend(self.create_instance(name, region, flavor, image, ssh_key, num))

        self.log.info('Sent request to OVH Cloud API to create {} instances'.format(len(self.instances)))

        wait = True
        while wait:
            time.sleep(5)
            wait = False
            for instance in (i for i in self.instances if 'ip' not in i):
                r = self.ovh.get('/cloud/project/{}/instance/{}'.format(self.project_id, instance['id']))
                if r['status'] == 'BUILD':
                    self.log.debug('Instance {} is still being build'.format(instance['id']))
                    wait = True
                elif r['status'] == 'ACTIVE':
                    ip = r['ipAddresses'][0]['ip']
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    if sock.connect_ex((ip, 22)) == 0:
                        instance['ip'] = ip
                        self.log.info(
                            'Instance {} is active with IP {} and ssh is available'.format(instance['id'], ip))
                    else:
                        self.log.debug(
                            'Instance {} is active with IP {} but ssh is not yet available'.format(instance['id'], ip))
                        wait = True
                elif r['status'] == 'ERROR':
                    self.log.error(
                        'Instance {} entered an ERROR state - dumping response object\n{}'.format(instance['id'], r))
                    self.recover_instance_error(instance['id'], name, region, flavor, image, ssh_key)
                    wait = True
                    break
                else:
                    self.log.error('Instance {} has an unexpected status {}'.format(instance['id'], r['status']))


class OVHClient(ovh.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @retry(stop_max_attempt_number=3)
    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)

    @retry(stop_max_attempt_number=3)
    def delete(self, *args, **kwargs):
        return super().delete(*args, **kwargs)


if __name__ == "__main__":
    main(sys.argv[1:])
