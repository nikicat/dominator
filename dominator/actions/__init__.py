import logging
import logging.config
import sys
import os
import pkg_resources
import datetime

import yaml
import docopt
import mako.template
from colorama import Fore

from ..entities import SourceImage
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
def start(shipment, shipname: str=None, containername: str=None):
    """Push images, render config volumes and Start containers

    Usage: dominator start [options] [<shipname>] [<containername>]

    Options:
        -h, --help
    """

    containers = shipment.filter_containers(shipname, containername)

    for cont in containers:
        cont.run()


@command
def restart(shipment, shipname: str=None, containername: str=None):
    """Restart containers

    Usage: dominator restart [options] [<shipname>] [<containername>]

    Options:
        -h, --help
    """
    for cont in shipment.filter_containers(shipname, containername):
        cont.check()
        if cont.running:
            cont.stop()
        cont.start()


@command
def exec(shipment, shipname: str, containername: str, keep: bool=False):
    """Start container, attach to process, read stdout/stderr
       and print it, then (optionally) remove it

    Usage: dominator exec [options] <shipname> <containername>

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
    dist = pkg_resources.get_distribution(distribution)
    assert dist is not None, "Could not load distribution for {}".format(distribution)

    if entrypoint is None:
        entrypoint = list(dist.get_entry_map('obedient').keys())[0]
        getlogger().debug("autodetected entrypoint is %s", entrypoint)

    func = dist.load_entry_point('obedient', entrypoint)
    assert func is not None, "Could not load entrypoint {} from distribution {}".format(entrypoint, distribution)

    import pkginfo
    meta = pkginfo.get_metadata(distribution)

    shipment = func()

    shipment.version = meta.version
    shipment.author = meta.author
    shipment.author_email = meta.author_email
    shipment.home_page = meta.home_page

    import tzlocal
    shipment.timestamp = datetime.datetime.now(tz=tzlocal.get_localzone())

    return shipment


def load_from_yaml(filename):
    getlogger().info("loading config from yaml", path=filename)
    if filename == '-':
        return yaml.load(sys.stdin)
    else:
        with open(filename) as f:
            return yaml.load(f)


@command
def status(shipment, shipname: str=None, containername: str=None, showdiff: bool=False):
    """Show shipment's status

    Usage: dominator status [options] [<shipname>] [<containername>]

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
        print('{c.shipment.name:20.20} {c.ship.name:10.10} {c.name:20.20} {color}{c.id:10.7} {c.status:30.30}{reset}'
              .format(c=c, color=color, reset=Fore.RESET))
        if c.running and showdiff:
            print_diff(diff)


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
        exec                start and attach to container locally
        build               build image
        images              show images list

    Options:
        -s, --settings <settings>          yaml file to load settings
        -l, --loglevel <loglevel>          log level [default: warn]
        -c, --config <config>              yaml config file
        -d, --distribution <distribution>  distribution name
        -e, --entrypoint <entrypoint>      entry point, by default uses first found
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
