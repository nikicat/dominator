"""
Usage: dominator [-s <settings>] [-l <loglevel>] (-c <config>|-m <module> [-f <function>]) \
                 [--no-cache] [--clear-cache] [-n <namespace>] <command> [<args>...]

Commands:
    dump                dump config in yaml format
    list-containers     list local containers (used by upstart script)
    makedeb             make debian/ dir ready to create obedient package
    start               start containers
    stop                stop containers
    restart             restart containers
    status              show containers' status

    localstart          start containers locally
    localstop           stop containers locally
    localrestart        restart containers locally
    localstatus         show local containers' status
    localexec           start and attach to container locally

Options:
    -s, --settings <settings>    yaml file to load settings
    -l, --loglevel <loglevel>    log level [default: warn]
    -c, --config <config>        yaml config file
    -m, --module <modulename>    python module name
    -f, --function <funcname>    python function name [default: create]
    -n, --namespace <namespace>  docker namespace to use if not set (overrides config)
    --no-cache                   disable requests cache when using -m/-f
    --clear-cache                clear requests cache (requires no --no-cache)
"""


import logging
import logging.config
import sys
import re
import os
import importlib
import itertools
from contextlib import contextmanager
import pkg_resources

import yaml
import docopt
import mako.template
from colorama import Fore

from ..entities import Container, Image, SourceImage, DataVolume
from .. import utils
from ..utils import getlogger, settings


def literal_str_representer(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|' if '\n' in data else None)
yaml.add_representer(str, literal_str_representer)


def command(func):
    func.iscommand = True
    return func


@command
def dump(containers):
    """Dump config as YAML

    Usage: dominator dump
    """
    print(yaml.dump(containers))


@command
def localstart(containers, shipname: str=None, containername: str=None):
    """Start locally all or specified containers

    Usage: dominator localstart [options] [<shipname>] [<containername>]

    Options:
        -h, --help
    """
    for cont in filter_containers(containers, shipname, containername):
        cont.run()


@command
def localrestart(containers, shipname: str=None, containername: str=None):
    """Restart locally all or specified containers

    Usage: dominator localrestart [options] [<shipname>] [<containername>]

    Options:
        -h, --help
    """
    for cont in filter_containers(containers, shipname, containername):
        cont.check()
        if cont.running:
            cont.stop()
        cont.start()


@command
def localexec(containers, shipname: str, containername: str, keep: bool=False):
    """Start container locally, attach to process, read stdout/stderr
       and print it, then (optionally) remove it

    Usage: dominator localexec [options] <shipname> <containername>

    Options:
        -h, --help
        -k, --keep  # keep container after stop [default: false]
    """
    for cont in filter_containers(containers, shipname, containername):
        try:
            with cont.execute() as logs:
                for line in logs:
                    print(line)
        finally:
            try:
                if not keep:
                    cont.remove(force=True)
            except:
                getlogger().exception("failed to remove container")


@command
def stop(containers, ship: str=None, container: str=None):
    """Stop container(s) on ship(s)

    Usage: dominator stop [options] [<ship> [<container>]]

    Options:
        -h, --help
    """
    for cont in filter_containers(containers, ship, container):
        cont.check()
        if cont.running:
            cont.stop()


def group_containers(containers, shipname: str=None, containername: str=None):
    return [(s, list(sconts)) for s, sconts in itertools.groupby(
        filter_containers(containers, shipname, containername), lambda c: c.ship)]


@utils.makesorted(lambda c: (c.ship.name, c.name))
def filter_containers(containers, shipname: str=None, containername: str=None):
    notfound = True
    for cont in containers:
        if ((shipname is None or re.match(shipname, cont.ship.name)) and
           (containername is None or re.match(containername, cont.name))):
            notfound = False
            yield cont
    if notfound:
        getlogger(shipname=shipname, containername=containername).error('no containers matched')


@command
def list_containers(containers, shipname: str=None):
    """Print container names
    Usage: dominator list-containers [options] [<shipname>]

    Options:
        -h, --help
    """
    for cont in filter_containers(containers, shipname):
        print(cont.name)


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
def localstatus(containers, ship: str=None, container: str=None, showdiff: bool=False):
    """Show local containers' status

    Usage: dominator localstatus [options] [<ship>] [<container>]

    Options:
        -h, --help
        -d, --showdiff  # show diff with running container [default: false]
    """
    for c in filter_containers(containers, ship, container):
        c.check()
        if c.running:
            diff = list(utils.compare_container(c, c.inspect()))
            getlogger().debug('compare result', diff=diff)
            if len(diff) > 0:
                color = Fore.YELLOW
            else:
                color = Fore.GREEN
        else:
            color = Fore.RED
        print('{c.ship.name:10.10} {c.name:20.20} {color}{c.id:10.7} {c.status:30.30}{reset}'.format(
            c=c,
            color=color,
            reset=Fore.RESET,
        ))
        if c.running and showdiff:
            print_diff(diff)


@command
def status(containers, ship: str=None, container: str=None, showdiff: bool=False, keep: bool=True):
    """Show local containers' status

    Usage: dominator status [options] [<ship>] [<container>]

    Options:
        -h, --help
        -d, --showdiff  # show diff with running container [default: false]
        -k, --keep      # keep ambassador container [default: false]
    """
    for s, containers in group_containers(containers, ship, container):
        getlogger(ship=s).debug("processing ship")
        runremotely(containers, s, 'localstatus' + (' -d' if showdiff else ''), keep, printlogs=True)


def print_diff(difflist):
    fore = Fore
    for key, diff in difflist:
        keystr = ' '.join(key)
        if isinstance(diff, list):
            # files diff
            for line in diff:
                color = {'- ': Fore.RED, '+ ': Fore.GREEN, '? ': Fore.BLUE}.get(line[:2], '')
                print('  {keystr:60.60} {color}{line}{fore.RESET}'.format(**locals()))
        elif len(diff) == 2:
            expected, actual = diff
            print('  {keystr:60.60} {fore.RED}{actual!s:50.50}{fore.RESET} \
{fore.GREEN}{expected!s:50.50}{fore.RESET}'.format(**locals()))
        else:
            assert False, "invalid diff format for {key}: {diff}".format(**locals())


@command
def start(containers, shipname: str=None, containername: str=None, keep: bool=False):
    """Start containers on ship[s]

    Usage: dominator start [options] [<shipname> [<containername>]]

    Options:
        -h, --help
        -k, --keep  # keep ambassador container after run
    """
    for image in {container.image for container in filter_containers(containers, shipname, containername)}:
        image.getid()
        image.push()

    for ship, containers in group_containers(containers, shipname, containername):
        runremotely(containers, ship, 'localstart', keep)


@command
def restart(containers, ship: str=None, container: str=None, keep: bool=False):
    """Restart selected containers

    Usage: dominator restart [options] [<ship>] [<container>]

    Options:
        -h, --help
        -k, --keep  # keep ambassador container after run
    """
    for s, containers in group_containers(containers, ship, container):
        runremotely(containers, s, 'localrestart', keep)


@command
def makedeb(containers, servicename: str):
    """Create debian/ directory in current dir ready for building debian package

    Usage: dominator makedeb [options] <servicename>

    Options:
        -h, --help
    """
    def render_dir(name):
        os.makedirs(name)
        for file in pkg_resources.resource_listdir(__name__, name):
            path = os.path.join(name, file)
            if pkg_resources.resource_isdir(__name__, path):
                render_dir(path)
            else:
                data = pkg_resources.resource_string(__name__, path)
                template = mako.template.Template(data)
                utils.getlogger().debug('rendering file %s', path)
                rendered = template.render(service=servicename)
                with open(path, 'w+') as output:
                    output.write(rendered)

    render_dir('debian')

    with open('debian/{}.yaml'.format(servicename), 'w+') as config:
        yaml.dump(containers, config)


@utils.cached
def getambassadorimage():
    image = SourceImage(
        name='dominator',
        parent=Image('yandex/trusty'),
        scripts=[
            'apt-get install -yyq python3-pip strace git mercurial',
            'pip3 install dominator[dump,colorlog]=={}'.format(getversion()),
        ],
        files={
            '/etc/dominator/settings.yaml': 'settings.docker.yaml',
        },
        volumes={
            'data': '/var/lib/dominator',
            'socket': '/run/docker.sock',
        },
        command='dominator -l debug -c - run',
    )
    image.getid()
    image.push()
    return image

def ambassador(ship, command):

    return Container(
        name='dominator-ambassador',
        image=getambassadorimage(),
        ship=ship,
        hostname=ship.name,
        command='dominator -c - {}'.format(command),
        volumes={
            'data': DataVolume(path='/var/lib/dominator', dest='/var/lib/dominator'),
            'docker': DataVolume(path='/run/docker.sock', dest='/run/docker.sock'),
        },
    )


def runremotely(containers, ship, command, keep: bool=False, printlogs: bool=False):
    logger = getlogger(ship=ship, command=command, keep=keep)
    logger.info('running remotely')

    if not printlogs:
        command = '-ldebug ' + command

    cont = ambassador(ship, command)

    with cont.execute() as logs:
        with _docker_attach(ship.docker, cont) as stdin:
            logger.debug('attached to stdin, sending config')
            stdin.send(yaml.dump(containers).encode())
            logger.debug('config sent, detaching stdin')

        logger = utils.getlogger('dominator.docker.logs', container=cont)
        for line in logs:
            if printlogs:
                print(line)
            else:
                logger.debug(line)

    if not keep:
        cont.remove()


@contextmanager
def _docker_attach(dock, cont):
    """some hacks to workaround docker-py bugs"""
    u = dock._url('/containers/{0}/attach'.format(cont.id))
    r = dock._post(u, params={'stdin': 1, 'stream': 1}, stream=True)
    yield r.raw._fp.fp.raw._sock
    r.close()


@command
def logs(containers, ship: str=None, container: str=None, follow: bool=False):
    """Fetch logs for container(s) on ship(s)

    Usage: dominator logs [options] [<ship>] [<container>]

    Options:
        -h, --help
        -f, --follow  # follow logs
    """
    for cont in filter_containers(containers, ship, container):
        cont.check()
        cont.logs(follow=follow)


@command
def build(containers, imagename: str=None, nocache: bool=False, push: bool=False):
    """Build source images

    Usage: dominator build [options] [<imagename>]

    Options:
        -h, --help
        -n, --nocache     # disable Docker cache [default: false]
        -p, --push        # push image to registry after build [default: false]
    """
    for cont in containers:
        if isinstance(cont.image, SourceImage) and (imagename is None or cont.image.repository == imagename):
            cont.image.build(push=push, nocache=nocache)


def getversion():
    try:
        return pkg_resources.get_distribution('dominator').version
    except pkg_resources.DistributionNotFound:
        return '(local)'


def main():
    args = docopt.docopt(__doc__, version=getversion(), options_first=True)
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
                if args['--no-cache']:
                    getlogger().info('loading containers without cache')
                    containers = load_module(args['--module'], args['--function'])
                else:
                    import requests_cache
                    with requests_cache.enabled():
                        if args['--clear-cache']:
                            getlogger().info("clearing requests cache")
                            requests_cache.clear()
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
