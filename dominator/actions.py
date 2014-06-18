"""
Usage: dominator [-s <settings>] [-l <loglevel>] (-c <config>|-m <module> [-f <function>]) [-n <namespace>] \
 <command> [<args>...]

Commands:
    dump                dump config in yaml format
    list-containers     list local containers (used by upstart script)
    run                 run container(s) locally
    deploy              deploy containers to ships
    status              show containers' status

Options:
    -s, --settings <settings>    yaml file to load settings
    -l, --loglevel <loglevel>    log level [default: warn]
    -c, --config <config>        yaml config file
    -m, --module <modulename>    python module name
    -f, --function <funcname>    python function name
    -n, --namespace <namespace>  docker namespace to use if not set (overrides config)
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
from structlog import get_logger
from structlog.threadlocal import tmp_bind
from colorama import Fore

import dominator
from .entities import ConfigVolume
from .settings import settings
from .utils import pull_repo, get_docker, compare_container


def literal_str_representer(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|' if '\n' in data else None)
yaml.add_representer(str, literal_str_representer)


def command(func):
    func.iscommand = True
    return func


@command
def dump(containers):
    """
    Dump config as YAML

    usage: dominator dump
    """
    print(yaml.dump(containers))


@command
def run(containers, container: str=None, remove: bool=False, detach: bool=True, dockerurl: str=None):
    """
    Run locally all or specified containers from config

    usage: dominator run [options] [<container>]

        -h, --help
        -r, --remove     # remove container after stop [default: false]
        -u, --dockerurl  # Docker API endpoint [default: null]
        -d, --detach     # do not follow container logs
    """
    for c in containers:
        if c.ship.islocal and (container is None or c.name == container):
            run_container(c, remove, detach if container is not None else True, dockerurl)


def _ps(dock, name, **kwargs):
    return [cont for cont in dock.containers(**kwargs) if cont['Names'][0][1:] == name]


def run_container(cont, remove: bool=False, detach: bool=True, dockerurl: str=None):
    logger = get_logger(container=cont)
    logger.info('starting container')

    dock = get_docker(dockerurl)

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

    image = cont.image
    # Check if ship has needed image
    if image.id not in [iinfo['Id'] for iinfo in dock.images(name=image.repository)]:
        logger.info('could not find requested image, pulling repo')
        pull_repo(dock, image.repository, image.id)

    running = _ps(dock, cont.name)
    if len(running) > 0:
        logger.info('found running container with the same name, comparing config with requested')
        diff = compare_container(cont, dock.inspect_container(running[0]))
        if len(diff) > 0:
            logger.info('running container config differs from requested, stopping', diff=diff)
            dock.stop(running[0])
        else:
            logger.info('running container config identical to requested, keeping')
            return

    stopped = _ps(dock, cont.name, all=True)
    if len(stopped):
        logger.info('found stopped container with the same name, removing')
        dock.remove_container(stopped[0])

    logger.info('creating container')
    cont_info = dock.create_container(
        image='{}:{}'.format(image.repository, image.id),
        hostname='{}-{}'.format(cont.name, cont.ship.name),
        command=cont.command,
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

    logger.info('container started')

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


@command
def list_containers(containers):
    """ list containers for local ship
    usage: dominator list-containers [-h]

        -h, --help
    """
    for container in containers:
        if container.ship.islocal:
            print(container.name)


def load_module(modulename, func):
    get_logger().info("loading config from module", module=modulename, func=func)
    module = importlib.import_module(modulename)
    return getattr(module, func)()


def load_yaml(filename):
    get_logger().info("loading config from yaml", filename=filename)
    if filename == '-':
        return yaml.load(sys.stdin)
    else:
        with open(filename) as f:
            return yaml.load(f)


@command
def status(containers, ship: str=None, showdiff: bool=False):
    """Show containers' status
    usage: dominator status [options] [<ship>]

        -h, --help
        -d, --showdiff  # show diff with running container [default: false]
    """
    for s in set([c.ship for c in containers]):
        if s.name == ship or ship is None:
            dock = _connect_to_ship(s)
            print('{}:'.format(s.name))
            ship_containers = dock.containers(all=True)
            for c in s.containers(containers):
                matched = [cinfo for cinfo in ship_containers if cinfo['Names'][0][1:] == c.name]
                color = Fore.RED
                if len(matched) == 0:
                    status = 'not found'
                    contid = ''
                else:
                    cinfo = matched[0]
                    status = cinfo['Status']
                    contid = cinfo['Id']
                    if 'Up' in status:
                        diff = compare_container(c, dock.inspect_container(cinfo))
                        get_logger().debug('compare result', diff=diff)
                        if len(diff) > 0:
                            color = Fore.YELLOW
                        else:
                            color = Fore.GREEN
                print('  {name:20.20} {id:10.7} {color}{status:30.30}{reset}'.format(
                    name=c.name,
                    status=status,
                    color=color,
                    id=contid,
                    reset=Fore.RESET,
                ))
                if len(matched) > 0 and showdiff:
                    print_diff(2, diff)


def print_diff(indent, diff):
    indentstr = '  '*indent
    for item in diff:
        if isinstance(item, str):
            # diff line
            color = {'- ': Fore.RED, '+ ': Fore.GREEN, '? ': Fore.BLUE}.get(item[:2], '')
            print('{indent}{color}{item}{reset}'.format(indent=indentstr, item=item, color=color, reset=Fore.RESET))
        elif len(item) == 3:
            # (key, expected, actual) tuple
            print('{indent}{key:30.30} {fore.RED}{actual:20.20}{fore.RESET} \
{fore.GREEN}{expected:20.20}{fore.RESET}'.format(indent=indentstr, fore=Fore,
                  key=item[0], expected=str(item[1]), actual=str(item[2])))
        elif len(item) == 2 and len(item[1]) > 0:
            # (key, list-of-subkeys) tuple
            print('{indent}{key:30.30}'.format(indent=indentstr, key=item[0]+':'))
            print_diff(indent+1, item[1])
        else:
            assert False, "invalid item {} in diff {}".format(item, diff)


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


@command
def deploy(containers, ship: str, keep: bool, pull: bool):
    """Deploy containers to ship[s]
    usage: dominator deploy [options] [<ship>]

        -h, --help
        -k, --keep  # keep configuration container after deploy
        -p, --pull  # pull deploy image before running
    """
    for s in {c.ship for c in containers}:
        if ship is None or s.name == ship:
            deploy_to_ship(s, containers, keep, pull)


def deploy_to_ship(ship, containers, keep, pull):
    logger = get_logger().bind(ship=ship)
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
    """some hacks to workaround docker-py bugs"""
    u = dock._url('/containers/{0}/attach'.format(cinfo['Id']))
    r = dock._post(u, params={'stdin': 1, 'stream': 1}, stream=True)
    yield r.raw._fp.fp.raw._sock
    r.close()


def initlog():
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


def main():
    args = docopt.docopt(__doc__, version=dominator.__version__, options_first=True)
    command = args['<command>']
    argv = [command] + args['<args>']
    commandfunc = getattr(sys.modules[__name__], command.replace('-', '_'), None)
    if not hasattr(commandfunc, 'iscommand'):
        exit("no such command, see 'dominator help'.")
    else:
        initlog()
        logging.basicConfig(level=getattr(logging, args['--loglevel'].upper()))
        settings.load(args['--settings'])
        if args['--namespace']:
            settings['docker-namespace'] = args['--namespace']
        logging.config.dictConfig(settings.get('logging', {}))
        if args['--config'] is not None:
            containers = load_yaml(args['--config'])
        else:
            containers = load_module(args['--module'], args['--function'])
        commandargs = docopt.docopt(commandfunc.__doc__, argv=argv)

        def pythonize_arg(arg):
            return arg.replace('--', '').replace('<', '').replace('>', '')
        commandfunc(containers, **{pythonize_arg(k): v for k, v in commandargs.items()
                                   if k not in ['--help', command]})
