"""
Usage: dominator [-s <settings>] [-l <loglevel>] (-c <config>|-m <module> [-f <function>]) <command> [<args>...]

Commands:
    dump                dump config in yaml format
    list-containers     list local containers (used by upstart script)
    run                 run container(s) locally
    deploy              deploy containers to ships
    status              show containers' status

Options:
    -s, --settings <settings>   yaml file to load settings
    -l, --loglevel <loglevel>   log level [default: warn]
    -c, --config <config>       yaml config file
    -m, --module <modulename>   python module name
    -f, --function <funcname>   python function name
"""


import logging
import logging.config
import os
import sys
import importlib
from contextlib import contextmanager

import yaml
import docopt
import structlog
import structlog.threadlocal
from structlog.threadlocal import tmp_bind

import dominator
from .entities import ConfigVolume
from .settings import settings
from .utils import pull_repo

_logger = structlog.get_logger()


def literal_str_representer(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|' if '\n' in data else None)
yaml.add_representer(str, literal_str_representer)


def dump(containers):
    """
    Dump config as YAML

    usage: dominator dump
    """
    print(yaml.dump(containers))


def run(containers, container: str, remove: bool, pull: bool, detach: bool):
    """
    Run locally all or specified containers from config

    usage: dominator run [options] [<container>]

        -h, --help
        -p, --pull    # pull repositories before start [default: false]
        -r, --remove  # remove container after stop [default: false]
        -d, --detach  # do not follow container logs
    """
    for c in containers:
        if c.ship.islocal and (container is None or c.name == container):
            run_container(c, remove, pull, detach if container is not None else True)


def _ps(dock, name, **kwargs):
    return list([cont for cont in dock.containers(**kwargs) if cont['Names'][0][1:] == name])


def run_container(cont, remove, pull, detach):
    logger = _logger.bind(container=cont)
    logger.info('starting container')
    import docker

    if cont.ship.islocal:
        logger.info('connecting to local docker')
        dock = docker.Client()
    else:
        raise RuntimeError('could only run containers on local docker (because of volumes)')

    for volume in cont.volumes:
        if isinstance(volume, ConfigVolume):
            with tmp_bind(logger, volume=volume) as logger:
                logger.debug('rendering config volume')
                path = volume.getpath(cont)
                os.makedirs(path, exist_ok=True)

                for filename in os.listdir(path):
                    os.remove(os.path.join(path, filename))
                for file in volume.files:
                    file.dump(cont, volume)

    if pull or len(dock.images(name=cont.repository)) == 0:
        pull_repo(dock, cont.repository, cont.tag)

    running = _ps(dock, cont.name)
    if len(running) > 0:
        logger.info('found running container with the same name, stopping')
        dock.stop(running[0])

    stopped = _ps(dock, cont.name, all=True)
    if len(stopped):
        logger.info('found stopped container with the same name, removing')
        dock.remove_container(stopped[0])

    logger.info('creating container')
    cont_info = dock.create_container(
        image='{}:{}'.format(cont.repository, cont.tag),
        hostname='{}-{}'.format(cont.name, cont.ship.name),
        mem_limit=cont.memory,
        environment=cont.env,
        name=cont.name,
        ports=list(cont.ports.values()),
    )

    logger = logger.bind(contid=cont_info['Id'][:7])
    logger.info('starting container')
    dock.start(
        cont_info,
        port_bindings={
            '{}/{}'.format(port, cont.portproto.get(name, 'tcp')): ('::', cont.extports.get(name, port))
            for name, port in cont.ports.items()
        },
        binds={v.getpath(cont): {'bind': v.dest, 'ro': v.ro} for v in cont.volumes},
    )

    if not detach:
        logger.info('attaching to container')
        try:
            logger = logging.getLogger(cont.name)
            for line in lines(dock.logs(cont_info, stream=True)):
                logger.debug(line)
        except KeyboardInterrupt:
            logger.info('received keyboard interrupt')

        logger.info('stopping container')
        dock.stop(cont_info, timeout=2)

        if remove:
            logger.info('removing container')
            dock.remove_container(cont_info)


def list_containers(containers):
    """ list containers for local ship
    usage: dominator list-containers [-h]

        -h, --help
    """
    for container in containers:
        if container.ship.islocal:
            print(container.name)


def load_module(modulename, func):
    _logger.info("loading config from module", module=modulename, func=func)
    module = importlib.import_module(modulename)
    return getattr(module, func)()


def load_yaml(filename):
    _logger.info("loading config from yaml", filename=filename)
    if filename == '-':
        return yaml.load(sys.stdin)
    else:
        with open(filename) as f:
            return yaml.load(f)


def status(containers, ship: str):
    """Show containers' status
    usage: dominator status [options] [<ship>]

        -h, --help
    """
    for s in set([c.ship for c in containers]):
        if s.name == ship or ship is None:
            dock = _connect_to_ship(s)
            print('{}:'.format(s.name))
            ship_containers = dock.containers(all=True)
            for c in s.containers(containers):
                matched = list([cinfo for cinfo in ship_containers if cinfo['Names'][0][1:] == c.name])
                if len(matched) == 0:
                    print('  {:20.20} not found'.format(c.name))
                else:
                    print('  {:20.20} {:30.30} {:.7}'.format(
                        c.name,
                        matched[0]['Status'],
                        dock.inspect_container(matched[0])['Image']
                    ))


def lines(records):
    buf = ''
    for record in records:
        buf += record.decode()
        while '\n' in buf:
            line, buf = buf.split('\n', 1)
            yield line


def _connect_to_ship(ship):
    import docker
    return docker.Client('http://{}:4243/'.format(ship.fqdn))


def deploy(containers, ship: str, keep: bool, pull: bool):
    """Deploy containers to ship[s]
    usage: dominator deploy [options] [<ship>]

        -h, --help
        -k, --keep  # keep configuration container after deploy
        -p, --pull  # pull deploy image before running
    """
    for s in set([c.ship for c in containers]):
        if ship is None or s.name == ship:
            deploy_to_ship(s, containers, keep, pull)


def deploy_to_ship(ship, containers, keep, pull):
    logger = _logger.bind(ship=ship)
    logger.info('deploying')
    dock = _connect_to_ship(ship)
    image = settings['deploy-image']

    if pull or len(dock.images(name=image)) == 0:
        pull_repo(dock, image)

    cinfo = dock.create_container(
        image=image,
        hostname=ship.name,
        stdin_open=True,
        detach=False,
    )

    dock.start(
        cinfo,
        binds={path: {'bind': path} for path in ['/var/lib/dominator', '/run/docker.sock']}
    )
    with docker_attach(dock, cinfo) as stdin:
        stdin.send(yaml.dump(containers).encode())

    for line in lines(dock.logs(cinfo, stream=True)):
        logger.info(line)

    dock.wait(cinfo)
    if not keep:
        dock.remove_container(cinfo)


@contextmanager
def docker_attach(dock, cinfo):
    '''some hacks to workaround docker-py bugs'''
    u = dock._url('/containers/{0}/attach'.format(cinfo['Id']))
    r = dock._post(u, params={'stdin': 1, 'stream': 1}, stream=True)
    yield r.raw._fp.fp.raw._sock
    r.close()


def makedeb():
    pass


def main():
    args = docopt.docopt(__doc__, version=dominator.__version__, options_first=True)
    command = args['<command>']
    argv = [command] + args['<args>']
    action = getattr(sys.modules[__name__], command.replace('-', '_'), None)
    if not callable(action) or not hasattr(action, '__doc__'):
        exit("no such command, see 'dominator help'.")
    else:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.KeyValueRenderer(sort_keys=True, key_order=['event'])
            ],
            context_class=structlog.threadlocal.wrap_dict(dict),
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
        logging.basicConfig(level=getattr(logging, args['--loglevel'].upper()))
        settings.load(args['--settings'])
        logging.config.dictConfig(settings.get('logging', {}))
        if args['--config'] is not None:
            containers = load_yaml(args['--config'])
        else:
            containers = load_module(args['--module'], args['--function'])
        action_args = docopt.docopt(action.__doc__, argv=argv)

        def pythonize_arg(arg):
            return arg.replace('--', '').replace('<', '').replace('>', '')
        action(containers, **{pythonize_arg(k): v for k, v in action_args.items()
                              if k not in ['--help', command]})
