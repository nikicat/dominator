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
import sys
import importlib
import itertools
from contextlib import contextmanager

import yaml
import docopt
from colorama import Fore

import dominator
from .entities import Container, Image, DataVolume, LocalShip
from .settings import settings
from . import utils
from .utils import getlogger


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
def run(containers, container: str=None, remove: bool=False, attach: bool=False):
    """
    Run locally all or specified containers from config

    usage: dominator run [options] [<container>]

        -h, --help
        -r, --remove     # remove container after stop [default: false]
        -a, --attach     # attach to container logs
    """
    for cont in _filter_containers(containers, LocalShip().name, container):
        cont.run()
        if attach and container is not None:
            cont.logs()
            if remove:
                cont.remove()


@command
def stop(containers, ship: str=None, container: str=None):
    """
    Stop container(s) on ship(s)

    Usage: dominator stop [options] [<ship> [<container>]]

    Options:
        -h, --help
    """
    for cont in _filter_containers(containers, ship, container):
        cont.check()
        cont.stop()


@utils.makesorted(lambda c: (c.ship.name, c.name))
def _filter_containers(containers, shipname: str, containername: str=None):
    notfound = True
    for cont in containers:
        if (shipname is None or cont.ship.name == shipname) and (containername is None or cont.name == containername):
            notfound = False
            yield cont
    if notfound:
        getlogger(shipname=shipname, containername=containername).error('no containers matched')


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
    getlogger().info("loading config from module", configmodule=modulename, configfunc=func)
    module = importlib.import_module(modulename)
    return getattr(module, func)()


def load_yaml(filename):
    getlogger().info("loading config from yaml", path=filename)
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
    for s, containers in itertools.groupby(_filter_containers(containers, ship), lambda c: c.ship):
        getlogger(ship=s).debug("processing ship")
        print('{}:'.format(s.name))
        for c in containers:
            c.check()
            if c.running:
                diff = utils.compare_container(c, s.docker.inspect_container(c.id))
                getlogger().debug('compare result', diff=diff)
                if len(diff) > 0:
                    color = Fore.YELLOW
                else:
                    color = Fore.GREEN
            else:
                color = Fore.RED
            print('  {c.name:20.20} {color}{c.id:10.7} {c.status:30.30}{reset}'.format(
                c=c,
                color=color,
                reset=Fore.RESET,
            ))
            if c.running and showdiff:
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
            print('{indent}{key:30.30} {fore.RED}{actual:30.30}{fore.RESET} \
{fore.GREEN}{expected:30.30}{fore.RESET}'.format(indent=indentstr, fore=Fore,
                  key=item[0], expected=str(item[1]), actual=str(item[2])))
        elif len(item) == 2 and len(item[1]) > 0:
            # (key, list-of-subkeys) tuple
            print('{indent}{key:30.30}'.format(indent=indentstr, key=item[0]+':'))
            print_diff(indent+1, item[1])
        else:
            assert False, "invalid item {} in diff {}".format(item, diff)


@command
def deploy(containers, ship: str=None, container: str=None, keep: bool=False):
    """Deploy containers to ship[s]
    Usage: dominator deploy [options] [<ship> [<container>]]

    Options:
        -h, --help
        -k, --keep  # keep ambassador container after deploy
    """
    for s in {c.ship for c in containers}:
        if ship is None or s.name == ship:
            deploy_to_ship(s, _filter_containers(containers, s.name, container), keep)


def ambassadors(ships):
    return [Container(
            name='dominator-ambassador',
            image=Image(settings['deploy-image']),
            ship=ship,
            hostname=ship.name,
            volumes=[
                DataVolume(path='/var/lib/dominator', dest='/var/lib/dominator'),
                DataVolume(path='/run/docker.sock', dest='/run/docker.sock'),
            ]) for ship in ships]


def deploy_to_ship(ship, containers, keep):
    logger = getlogger(ship=ship)
    logger.info('deploying containers to ship')

    deploycont = ambassadors([ship])[0]

    deploycont.run()

    with _docker_attach(ship.docker, deploycont) as stdin:
        logger.debug('attached to stdin, sending config')
        stdin.send(yaml.dump(containers).encode())
        logger.debug('config sent, detaching stdin')

    deploycont.logs()

    if not keep:
        deploycont.stop()
        deploycont.remove()


@contextmanager
def _docker_attach(dock, cont):
    """some hacks to workaround docker-py bugs"""
    u = dock._url('/containers/{0}/attach'.format(cont.id))
    r = dock._post(u, params={'stdin': 1, 'stream': 1}, stream=True)
    yield r.raw._fp.fp.raw._sock
    r.close()


@command
def logs(containers, ship: str=None, container: str=None):
    """
    Fetch logs for container(s) on ship(s)

    Usage: dominator logs [options] [<ship>] [<container>]

    Options:
        -h, --help
    """
    for cont in _filter_containers(containers, ship, container):
        cont.check()
        cont.logs()


def main():
    args = docopt.docopt(__doc__, version=dominator.__version__, options_first=True)
    command = args['<command>']
    argv = [command] + args['<args>']
    commandfunc = getattr(sys.modules[__name__], command.replace('-', '_'), None)
    if not hasattr(commandfunc, 'iscommand'):
        exit("no such command, see 'dominator help'.")
    else:
        loglevel = getattr(logging, args['--loglevel'].upper())
        logging.basicConfig(level=loglevel)
        settings.load(args['--settings'])
        if args['--namespace']:
            settings['docker-namespace'] = args['--namespace']
        logging.config.dictConfig(settings.get('logging', {}))
        logging.disable(level=loglevel-1)
        try:
            if args['--config'] is not None:
                containers = load_yaml(args['--config'])
            else:
                containers = load_module(args['--module'], args['--function'])
        except:
            getlogger().exception("failed to load config")
            return
        commandargs = docopt.docopt(commandfunc.__doc__, argv=argv)

        def pythonize_arg(arg):
            return arg.replace('--', '').replace('<', '').replace('>', '')
        try:
            commandfunc(containers, **{pythonize_arg(k): v for k, v in commandargs.items()
                                       if k not in ['--help', command]})
        except:
            getlogger(command=command).exception("failed to execute command")
            return
