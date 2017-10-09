import os
import pipes
import subprocess
import time

from charmhelpers.core import (
    hookenv,
    templating,
)
from charms.reactive import (
    hook,
    only_once,
    remove_state,
    set_state,
    when,
)
import yaml


CURDIR = os.getcwd()
FILES = os.path.join(CURDIR, 'files')
AGENT = os.path.join(CURDIR, '..', 'agent.conf')
IMAGE_NAME = 'termserver'
LXC = '/snap/bin/lxc'
BRIGE_NAME = 'jujushellbr0'


def call(command, *args, **kwargs):
    """Call a subprocess passing the given arguments.

    Take the subcommand and its parameters as args.
    Raise an OSError with the error output in case of failure.
    """
    pipe = subprocess.PIPE
    cmd = (command,) + args
    cmdline = ' '.join(map(pipes.quote, cmd))
    hookenv.log('running the following: {!r}'.format(cmdline))
    try:
        process = subprocess.Popen(
            cmd, stdin=pipe, stdout=pipe, stderr=pipe, **kwargs)
    except OSError as err:
        raise OSError('command {!r} not found: {}'.format(command, err))
    output, error = map(lambda msg: msg.decode('utf-8'), process.communicate())
    retcode = process.poll()
    if retcode:
        msg = 'command {!r} failed with retcode {}: {!r}'.format(
            cmdline, retcode, output + error)
        hookenv.log(msg)
        raise OSError(msg)
    hookenv.log('command {!r} succeeded: {!r}'.format(cmdline, output))


def build_config():
    """Build and save the jujushell server config."""
    hookenv.log('building jujushell config.yaml')
    api_addrs = os.environ.get('JUJU_API_ADDRESSES')
    if api_addrs is None:
        raise ValueError('could not find API addresses')
    cfg = hookenv.config()
    data = {
        'juju-addrs': api_addrs.split(),
        'juju-cert': get_juju_cert(AGENT),
        'image-name': IMAGE_NAME,
        'log-level': cfg['log-level'],
        'port': cfg['port'],
    }
    with open(os.path.join(FILES, 'config.yaml'), 'w') as stream:
        yaml.safe_dump(data, stream=stream)


def get_juju_cert(path):
    """Return the certificate to use when connecting to the controller.

    The certificate is provided in PEM format and it is retrieved by parsing
    agent.conf.
    """
    with open(path) as stream:
        return yaml.safe_load(stream)['cacert']


def manage_ports():
    """Opens the port on which to listen, closing the previous if needed."""
    cfg = hookenv.config()
    if cfg.changed('port'):
        hookenv.log('port updated from {} to {}'.format(
            cfg.previous('port'), cfg['port']))
        hookenv.close_port(cfg.previous('port'))
    hookenv.open_port(cfg['port'])
    build_config()


def restart():
    """Restarts the jujushell service."""
    hookenv.status_set('maintenance', '(re)starting the jujushell service')
    manage_ports()
    call('systemctl', 'restart', 'jujushell.service')
    hookenv.status_set('active', 'jujushell started')
    set_state('jujushell.started')
    remove_state('jujushell.stopped')
    hookenv.status_set('active', 'jujushell is ready')


def save_resource(name, path):
    """Retrieve a resource with the given name and save it in the given path.

    Raise an OSError if the resource cannot be retrieved.
    """
    hookenv.log('retrieving resource {!r}'.format(name))
    resource = hookenv.resource_get(name)
    if not resource:
        msg = 'cannot retrieve resource {!r}'.format(name)
        hookenv.log(msg)
        raise OSError(msg)
    os.rename(resource, path)
    hookenv.log('resource {!r} saved at {!r}'.format(name, path))


@hook('install')
def install_service():
    """Installs the jujushell systemd service."""
    # Render the jujushell systemd service module.
    hookenv.status_set('maintenance', 'creating systemd module')
    templating.render(
        'jujushell.service', '/usr/lib/systemd/user/jujushell.service', {
        'jujushell': os.path.join(FILES, 'jujushell'),
        'jujushell_config': os.path.join(FILES, 'config.yaml'),
    }, perms=775)
    # Retrieve the jujushell binary resource.
    binary = os.path.join(FILES, 'jujushell')
    save_resource('jujushell', binary)
    os.chmod(binary, 0o775)
    # Build the configuration file for jujushell.
    build_config()
    # Enable the jujushell module.
    hookenv.status_set('maintenance', 'enabling systemd module')
    call('systemctl', 'enable', '/usr/lib/systemd/user/jujushell.service')
    call('systemctl', 'daemon-reload')
    set_state('jujushell.installed')
    hookenv.status_set('maintenance', 'jujushell installed')


@when('snap.installed.lxd')
@only_once
def setup_lxd():
    """Configure LXD."""
    hookenv.status_set('maintenance', 'configuring group membership')
    call('adduser', 'ubuntu', 'lxd')

    # When running LXD commands, use a working directory that's surely
    # available also from the perspective of confined LXD.
    cwd = '/'

    try:
        call(LXC, 'network', 'show', 'jujushellbr0', cwd=cwd)
    except OSError:
        # LXD is not yet initialized.
        hookenv.status_set('maintenance', 'setting up LXD')
        # Wait for the LXD daemon to be up and running.
        # TODO: we can do better than time.sleep().
        time.sleep(10)
        call(_LXD_INIT_COMMAND, shell=True, cwd=cwd)
        hookenv.log('lxd initialized')
    else:
        hookenv.log('lxd already initialized')

    try:
        call(LXC, 'image', 'show', 'termserver', cwd=cwd)
    except OSError:
        # The image has not been imported yet.
        hookenv.status_set('maintenance', 'fetching LXD image')
        image = '/tmp/termserver.tar.gz'
        save_resource('termserver', image)
        hookenv.status_set('maintenance', 'importing LXD image')
        call(LXC, 'image', 'import', image, '--alias', 'termserver', cwd=cwd)
        hookenv.log('lxd image imported')
    else:
        hookenv.log('lxd image already imported')
    hookenv.status_set('maintenance', 'LXD set up completed')


@hook('start')
def start():
    restart()


@hook('config-changed')
def config_changed():
    restart()


@hook('stop')
def stop():
    """Stops the jujushell service."""
    call('systemctl', 'stop', 'jujushell.service')
    remove_state('jujushell.started')
    set_state('jujushell.stopped')


# Define the command used to initialize LXD.
_LXD_INIT_COMMAND = """
cat <<EOF | /snap/bin/lxd init --preseed
networks:
- name: jujushellbr0
  type: bridge
  config:
    ipv4.address: auto
    ipv6.address: none
storage_pools:
- name: data
  driver: zfs
profiles:
- name: default
  devices:
    root:
      path: /
      pool: data
      type: disk
    eth0:
      name: eth0
      nictype: bridged
      parent: jujushellbr0
      type: nic
EOF
"""
