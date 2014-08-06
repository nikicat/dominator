import logging
import logging.config
import sys
import os
from contextlib import contextmanager
import pkg_resources

import yaml
import docopt
import mako.template
from colorama import Fore

from ..entities import Container, Image, SourceImage, DataVolume, Shipment
from .. import utils
from ..utils import getlogger, settings


def literal_str_representer(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|' if '\n' in data else None)
yaml.add_representer(str, literal_str_representer)


def command(func):
    func.iscommand = True
    return func


@command
def dump(shipment):
    """Dump config as YAML

    Usage: dominator dump
    """
    print(yaml.dump(shipment))


@command
def localstart(shipment, shipname: str=None, containername: str=None):
    """Start locally all or specified containers

    Usage: dominator localstart [options] [<shipname>] [<containername>]

    Options:
        -h, --help
    """
    for cont in shipment.filter_containers(shipname, containername):
        cont.run()


@command
def localrestart(shipment, shipname: str=None, containername: str=None):
    """Restart locally all or specified containers

    Usage: dominator localrestart [options] [<shipname>] [<containername>]

    Options:
        -h, --help
    """
    for cont in shipment.filter_containers(shipname, containername):
        cont.check()
        if cont.running:
            cont.stop()
        cont.start()


@command
def localexec(shipment, shipname: str, containername: str, keep: bool=False):
    """Start container locally, attach to process, read stdout/stderr
       and print it, then (optionally) remove it

    Usage: dominator localexec [options] <shipname> <containername>

    Options:
        -h, --help
        -k, --keep  # keep container after stop [default: false]
    """
    for cont in shipment.filter_containers(shipname, containername):
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
def stop(shipment, ship: str=None, container: str=None):
    """Stop container(s) on ship(s)

    Usage: dominator stop [options] [<ship> [<container>]]

    Options:
        -h, --help
    """
    for cont in shipment.filter_containers(ship, container):
        cont.check()
        if cont.running:
            cont.stop()


def group_containers(shipment, shipname: str=None, containername: str=None):
    return [(ship, shipment.filter_containers(ship.name, containername))
            for ship in shipment.ships if shipname is None or ship.name == shipname]


@command
def list_containers(shipment, shipname: str=None):
    """Print container names
    Usage: dominator list-containers [options] [<shipname>]

    Options:
        -h, --help
    """
    for cont in shipment.filter_containers(shipname):
        print(cont.name)


def load_from_distribution(distribution, entrypoint):
    getlogger().info("loading config from distribution entry point", distribution=distribution, entrypoint=entrypoint)
    return Shipment(distribution=distribution, entrypoint=entrypoint)


def load_from_yaml(filename):
    getlogger().info("loading config from yaml", path=filename)
    if filename == '-':
        return yaml.load(sys.stdin)
    else:
        with open(filename) as f:
            return yaml.load(f)


@command
def localstatus(shipment, shipname: str=None, containername: str=None, showdiff: bool=False):
    """Show local shipment' status

    Usage: dominator localstatus [options] [<shipname>] [<containername>]

    Options:
        -h, --help
        -d, --showdiff  # show diff with running container [default: false]
    """
    for c in shipment.filter_containers(shipname, containername):
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
def status(shipment, shipname: str=None, containername: str=None, showdiff: bool=False, keep: bool=True):
    """Show local shipment' status

    Usage: dominator status [options] [<shipname>] [<containername>]

    Options:
        -h, --help
        -d, --showdiff  # show diff with running container [default: false]
        -k, --keep      # keep ambassador container [default: false]
    """
    for ship, _ in group_containers(shipment, shipname, containername):
        getlogger(ship=ship).debug("processing ship")
        runremotely(shipment, ship, 'localstatus' + (' -d' if showdiff else ''), keep, printlogs=True)


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
def start(shipment, shipname: str=None, containername: str=None, keep: bool=False):
    """Start containers on ship[s]

    Usage: dominator start [options] [<shipname> [<containername>]]

    Options:
        -h, --help
        -k, --keep  # keep ambassador container after run
    """
    for image in {container.image for container in shipment.filter_containers(shipname, containername)}:
        image.getid()
        image.push()

    for ship, _ in group_containers(shipment, shipname, containername):
        runremotely(shipment, ship, 'localstart', keep)


@command
def restart(shipment, shipname: str=None, containername: str=None, keep: bool=False):
    """Restart selected containers

    Usage: dominator restart [options] [<shipname>] [<containername>]

    Options:
        -h, --help
        -k, --keep  # keep ambassador container after run
    """
    for ship, _ in group_containers(shipment, shipname, containername):
        runremotely(shipment, ship, 'localrestart', keep)


@command
def makedeb(shipment, packagename: str, distribution: str, urgency: str, target: str):
    """Create debian/ directory in current dir ready for building debian package

    Usage: dominator makedeb [options] <packagename> [<distribution>] [<urgency>]

    Options:
        -h, --help
        -t, --target  target directory to create debian/ inside [default: ./]
    """

    def render_dir(name):
        os.makedirs(os.path.join(target, name))
        for file in pkg_resources.resource_listdir(__name__, name):
            path = os.path.join(name, file)
            if pkg_resources.resource_isdir(__name__, path):
                render_dir(path)
            else:
                filename = pkg_resources.resource_filename(__name__, path)
                template = mako.template.Template(filename=filename)
                utils.getlogger().debug("rendering file %s", path)
                rendered = template.render(
                    packagename=packagename,
                    shipment=shipment,
                    distribution=distribution or 'unstable',
                    urgency=urgency or 'low',
                )
                with open(os.path.join(target, path), 'w+') as output:
                    output.write(rendered)

    render_dir('debian')

    with open(os.path.join(target, 'debian', '{}.yaml'.format(packagename)), 'w+') as config:
        yaml.dump(shipment, config)


@utils.cached
def getambassadorimage():
    return Image(settings['deploy-image'])


def getambassador(ship, command):
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


def runremotely(shipment, ship, command, keep: bool=False, printlogs: bool=False):
    if not printlogs:
        command = '-ldebug ' + command
    command = '{command} {ship.name}'.format(**locals())

    logger = getlogger(ship=ship, command=command, keep=keep)
    logger.info('running remotely')

    cont = getambassador(ship, command)

    with cont.execute() as logs:
        with _docker_attach(ship.docker, cont) as stdin:
            logger.debug('attached to stdin, sending config')
            stdin.send(yaml.dump(shipment).encode())
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
def logs(shipment, ship: str=None, container: str=None, follow: bool=False):
    """Fetch logs for container(s) on ship(s)

    Usage: dominator logs [options] [<ship>] [<container>]

    Options:
        -h, --help
        -f, --follow  # follow logs
    """
    for cont in shipment.filter_containers(ship, container):
        cont.check()
        cont.logs(follow=follow)


@command
def build(shipment, imagename: str, nocache: bool, push: bool, rebuild: bool):
    """Build source images

    Usage: dominator build [options] [<imagename>]

    Options:
        -h, --help
        -n, --nocache     # disable Docker cache [default: false]
        -p, --push        # push image to registry after build [default: false]
        -r, --rebuild     # rebuild image even if already built [default: false]
    """
    for image in shipment.images:
        if isinstance(image, SourceImage) and (imagename is None or image.repository == imagename):
            if rebuild:
                image.build(nocache=nocache)
            else:
                image.getid(nocache=nocache)
            if push:
                image.push()


@command
def images(shipment, imagename: str):
    """Print image list in build order

    Usage: dominator images [options] [<imagename>]

    Options:
        -h, --help
    """
    for image in shipment.images:
        if isinstance(image, SourceImage) and (imagename is None or image.repository == imagename):
            print(image)


def getversion():
    try:
        return pkg_resources.get_distribution('dominator').version
    except pkg_resources.DistributionNotFound:
        return '(local)'


def main():
    """
    Usage: dominator [-s <settings>] [-l <loglevel>] (-c <config>|-d <distribution> [-e <entrypoint>]) \
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
        -s, --settings <settings>          yaml file to load settings
        -l, --loglevel <loglevel>          log level [default: warn]
        -c, --config <config>              yaml config file
        -d, --distribution <distribution>  distribution name
        -e, --entrypoint <entrypoint>      entry point, by default uses first found [default: None]
        -n, --namespace <namespace>        docker namespace to use if not set (overrides config)
        --no-cache                         disable requests cache when using -m/-f
        --clear-cache                      clear requests cache (ignored with --no-cache)
    """

    args = docopt.docopt(main.__doc__, version=getversion(), options_first=True)
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
                shipment = load_from_yaml(args['--config'])
            else:
                if args['--no-cache']:
                    getlogger().info('loading containers without cache')
                    shipment = load_from_distribution(args['--distribution'], args['--entrypoint'])
                else:
                    import requests_cache
                    with requests_cache.enabled():
                        if args['--clear-cache']:
                            getlogger().info("clearing requests cache")
                            requests_cache.clear()
                        shipment = load_from_distribution(args['--distribution'], args['--entrypoint'])
        except:
            getlogger().exception("failed to load config")
            return
        commandargs = docopt.docopt(commandfunc.__doc__, argv=argv)

        def pythonize_arg(arg):
            return arg.replace('--', '').replace('<', '').replace('>', '')
        try:
            commandfunc(shipment, **{pythonize_arg(k): v for k, v in commandargs.items()
                        if k not in ['--help', command]})
        except:
            getlogger(command=command).exception("failed to execute command")
            sys.exit(1)
