import logging
import logging.config
import os
import pkg_resources
import datetime
import fnmatch
import re

import yaml
import mako.template
from colorama import Fore
import click

from ..entities import SourceImage, BaseShip, BaseFile, Volume, Container, Shipment
from .. import utils
from ..utils import getlogger


def literal_str_representer(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|' if '\n' in data else None)
yaml.add_representer(str, literal_str_representer)


def validate_loglevel(ctx, param, value):
    try:
        try:
            level = int(value)
        except ValueError:
            level = logging._nameToLevel[value.upper()]
        return level
    except ValueError:
        raise click.BadParameter('loglevel should be logging level name or number')


@click.group()
@click.option('-c', '--config', type=click.File('r'), help="file path to load config from")
@click.option('-s', '--settings', type=click.File('r'), help="file path to load settings from")
@click.option('-n', '--namespace', help="override docker namespace from settings")
@click.option('-l', '--loglevel', callback=validate_loglevel, default='warn')
@click.version_option()
@click.pass_context
def cli(ctx, config, loglevel, settings, namespace):
    logging.basicConfig(level=loglevel)
    utils.settings.load(settings)
    if namespace:
        utils.settings['docker-namespace'] = namespace
    logging.config.dictConfig(utils.settings.get('logging', {}))
    logging.disable(level=loglevel-1)

    if config is not None:
        shipment = yaml.load(config)
        shipment.make_backrefs()
        ctx.obj = shipment


@cli.group(chain=True)
def shipment():
    """Shipment management commands."""
    pass


def getobedients():
    return [pkgname for pkgname in pkg_resources.Environment() if pkgname.startswith('obedient.')]


@shipment.command()
@click.pass_context
@click.argument('distribution', required=False, type=click.Choice(getobedients()), metavar='<distribution>')
@click.argument('entrypoint', required=False, metavar='<entrypoint>')
@click.option('--cache/--no-cache', default=True)
@click.option('--clear-cache', is_flag=True, default=False, help="clear requests_cache before run (requires --cache)")
def generate(ctx, distribution, entrypoint, cache, clear_cache):
    """Generates yaml config file for shipment."""
    if distribution is None:
        click.echo('\n'.join(getobedients()))
        ctx.exit()

    dist = pkg_resources.get_distribution(distribution)

    if entrypoint is None:
        # Show all "obedient" entrypoints for package
        for entrypoint in dist.get_entry_map('obedient').keys():
            click.echo(entrypoint)
        ctx.exit()

    getlogger().info("generating config", distribution=distribution, entrypoint=entrypoint)

    assert dist is not None, "Could not load distribution for {}".format(distribution)

    if entrypoint is None:
        entrypoint = list(dist.get_entry_map('obedient').keys())[0]
        getlogger().debug("autodetected entrypoint is %s", entrypoint)

    func = dist.load_entry_point('obedient', entrypoint)
    assert func is not None, "Could not load entrypoint {} from distribution {}".format(entrypoint, distribution)

    import pkginfo
    meta = pkginfo.get_metadata(distribution)

    if cache:
        import requests_cache
        with requests_cache.enabled():
            if clear_cache:
                requests_cache.clear()
            shipment = func()
    else:
        getlogger().info('loading containers without cache')
        shipment = func()

    shipment.version = meta.version
    shipment.author = meta.author
    shipment.author_email = meta.author_email
    shipment.home_page = meta.home_page
    shipment.dominator_version = getversion()

    import tzlocal
    shipment.timestamp = datetime.datetime.now(tz=tzlocal.get_localzone())

    getlogger().debug("retrieving image ids")
    for image in shipment.images:
        if not isinstance(image, SourceImage):
            if image.getid() is None:
                image.pull()
                if image.getid() is None:
                    raise RuntimeError("Could not find id for image {}".format(image))

    click.echo_via_pager(yaml.dump(shipment))


@shipment.command()
@click.pass_obj
@click.argument('packagename')
@click.argument('distribution', default='unstable')
@click.argument('urgency', default='low')
@click.option('-t', '--target', type=click.Path(), default='./', help="target directory to create debian/ inside")
def makedeb(shipment, packagename, distribution, urgency, target):
    """Generate debian/ directory to make a .deb."""

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
                    distribution=distribution,
                    urgency=urgency,
                )
                with open(os.path.join(target, path), 'w+') as output:
                    output.write(rendered)

    render_dir('debian')

    with open(os.path.join(target, 'debian', '{}.yaml'.format(packagename)), 'w+') as config:
        yaml.dump(shipment, config)


@shipment.command()
@click.pass_obj
@click.argument('filename', required=False, type=click.Path())
def objgraph(shipment, filename):
    """Dump object graph using objgraph."""
    import objgraph

    def filter_entities(obj):
        return isinstance(obj, (BaseShip, BaseFile, Volume, Container, Shipment, dict, list))

    def highlight(obj):
        return not isinstance(obj, (dict, list))
    # Max depth is 14 because so many nodes are from Shipment (top) to File object (bottom) object
    # This value should be changed if graph depth changes
    objgraph.show_refs(shipment, filename=filename, max_depth=14, filter=filter_entities, highlight=highlight)


@cli.group(chain=True)
@click.pass_context
@click.option('-p', '--pattern', default='*', help="pattern to filter ship:container")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
def container(ctx, pattern, regex):
    """Container management commands."""
    shipment = ctx.obj
    ctx.obj = filterbyname(shipment.containers, pattern, regex)


@container.command()
@click.pass_obj
def start(containers):
    """Push images, render config volumes and Start containers."""
    for cont in containers:
        cont.run()


@container.command()
@click.pass_obj
def restart(containers):
    """Restart containers."""
    for cont in containers:
        cont.check()
        if cont.running:
            cont.stop()
        cont.run()


@container.command()
@click.pass_obj
@click.option('-k', '--keep', is_flag=True, default=False, help="keep container after stop")
def exec(containers, keep):
    """Start, attach and wait a container."""
    for cont in containers:
        try:
            with cont.execute() as logs:
                for line in logs:
                    click.echo(line)
        finally:
            try:
                if not keep:
                    cont.remove(force=True)
            except:
                getlogger().exception("failed to remove container")


@container.command()
@click.pass_obj
def stop(containers):
    """Stop container(s) on ship(s)."""
    for cont in containers:
        cont.check()
        if cont.running:
            cont.stop()


@container.command('list')
@click.pass_obj
def list_containers(containers):
    """Print container names."""
    for container in containers:
        click.echo(container.fullname)


@container.command()
@click.pass_obj
@click.option('-d', '--showdiff', is_flag=True, default=False, help="show diff with running container")
def status(containers, showdiff):
    """Show container status."""
    for c in containers:
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
        click.echo('{c.fullname:60.60} {color}{id:10.7} {c.status:30.30}{reset}'
                   .format(c=c, color=color, id=c.id or '', reset=Fore.RESET))
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
                click.echo('  {keystr:60.60} {color}{line}{fore.RESET}'.format(**locals()))
        elif len(diff) == 2:
            expected, actual = diff
            click.echo('  {keystr:60.60} {fore.RED}{actual!s:50.50}{fore.RESET} '
                       '{fore.GREEN}{expected!s:50.50}{fore.RESET}'.format(**locals()))
        else:
            assert False, "invalid diff format for {key}: {diff}".format(**locals())


@container.command()
@click.pass_obj
@click.option('-f', '--follow', is_flag=True, default=False, help="follow logs")
def log(containers, follow):
    """View Docker log for container(s)."""
    for cont in containers:
        cont.check()
        for line in cont.logs(follow=follow):
            click.echo(line)


@container.command('dump')
@click.pass_obj
def dump_containers(containers):
    """Dump container info."""
    for container in containers:
        container.ship = None
        container.shipment = None
        for volume in container.volumes.values():
            if hasattr(volume, 'files'):
                for file in volume.files.values():
                    if hasattr(file, 'context'):
                        file.context = 'skipped'
        click.echo_via_pager(yaml.dump(container))


@cli.group()
@click.option('-p', '--pattern', default='*', help="pattern to filter files (ship:container:volume:file)")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
@click.pass_context
def file(ctx, pattern, regex):
    shipment = ctx.obj
    ctx.obj = list(filterbyname(shipment.files, pattern, regex))


@file.command('list')
@click.pass_obj
def list_files(files):
    """List files."""
    for file in files:
        click.echo('{file.fullname:60.60} {file.fullpath}'.format(file=file))


@file.command('view')
@click.pass_obj
def view_files(files):
    """View file via `less'."""
    for file in files:
        file.volume.container.ship.spawn('less -S {}'.format(file.fullpath))


@cli.group(chain=True)
@click.pass_context
@click.option('-p', '--pattern', default='*', help="pattern to filter images")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
def image(ctx, pattern, regex):
    """Image management commands."""
    shipment = ctx.obj
    images = []
    if not regex:
        pattern = fnmatch.translate(pattern)
    images = [image for image in shipment.images if re.match(pattern, image.repository)]
    ctx.obj = images


@image.command()
@click.pass_obj
@click.option('-n', '--nocache', is_flag=True, default=False, help="disable Docker cache")
@click.option('-p', '--push', is_flag=True, default=False, help="push image to registry after the build")
@click.option('-r', '--rebuild', is_flag=True, default=False, help="rebuild image even if alredy built (hashtag found)")
def build(images, nocache, push, rebuild):
    """Build source images."""
    for image in images:
        if not isinstance(image, SourceImage):
            continue
        # image.getid() == None means that image with given tag doesn't exist
        if rebuild or image.getid() is None:
            image.build(nocache=nocache)
        if push:
            image.push()


@image.command('list')
@click.pass_obj
def list_images(images):
    """Print image list in build order."""
    for image in images:
        click.echo(image)


@cli.group()
@click.pass_context
@click.option('-p', '--pattern', 'pattern', default='*', help="pattern to filter ships")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
def ship(ctx, pattern, regex):
    """Ship management commands."""
    shipment = ctx.obj
    ctx.obj = filterbyname(shipment.ships.values(), pattern, regex)


@ship.command('list')
@click.pass_obj
def list_ships(ships):
    """List ships in format "<name>      <fqdn>"."""
    for ship in ships:
        click.echo('{:15.15}{}'.format(ship.name, ship.fqdn))


@ship.command('restart')
@click.pass_obj
def restart_ship(ships):
    """Restart ship(s)."""
    for ship in ships:
        ship.restart()


@ship.group('container')
@click.pass_context
@click.option('-p', '--pattern', default='*', help="filter containers using pattern")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
def ship_container(ctx, pattern, regex):
    """Command to manage arbitary ships' containers."""
    ships = ctx.obj
    if not regex:
        pattern = fnmatch.translate(pattern)
    cinfos = []
    for ship in ships:
        for cinfo in ship.docker.containers():
            if re.match(pattern, cinfo['Names'][0][1:]):
                cinfos.append(cinfo)
                # Add Ship object ref to cinfo to use it in subcommands
                cinfo['ship'] = ship
    ctx.obj = cinfos


@ship_container.command('list')
@click.pass_obj
def list_ship_containers(cinfos):
    """Outputs list of all containers running on ships"""
    for cinfo in cinfos:
        click.echo('{ship.name} {Names[0]:40.40} {Status:15.15} {ports}'.format(
            ports=[port.get('PublicPort') for port in cinfo['Ports']], **cinfo
        ))


@ship_container.command('inspect')
@click.pass_obj
def inspect_ship_containers(cinfos):
    """Outputs detailed info about any running container(s) on a ship."""
    for cinfo in cinfos:
        cinfoext = cinfo['ship'].docker.inspect_container(cinfo)
        cinfoext['!ship'] = cinfo['ship'].name
        click.echo(yaml.dump(cinfoext))


@ship_container.command('log')
@click.pass_obj
@click.option('-f', '--follow', is_flag=True, default=False, help="follow logs")
def view_ship_container_log(cinfos, follow):
    """Outputs container logs for arbitary container on a ship."""
    for cinfo in cinfos:
        cont = Container(cinfo['Names'][0][1:], cinfo['ship'], None)
        cont.check(cinfo)
        for line in cont.logs(follow):
            click.echo(line)


@cli.group()
@click.option('-p', '--pattern', 'pattern', default='*', help="pattern to filter volumes (ship:container:volume)")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
@click.pass_context
def volumes(ctx, pattern, regex):
    """Commands to manage volumes."""
    shipment = ctx.obj
    ctx.obj = list(filterbyname(shipment.volumes, pattern, regex))


@volumes.command('list')
@click.pass_obj
def list_volumes(volumes):
    """List volumes."""
    for volume in volumes:
        click.echo('{volume.fullname:30.30} {volume.dest:30.30} {volume.fullpath}'.format(volume=volume))


@cli.group()
@click.pass_context
@click.option('-p', '--pattern', 'pattern', default='*', help="pattern to filter ships")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
def doors(ctx, pattern, regex):
    """Commands to view doors."""
    shipment = ctx.obj
    ctx.obj = list(filterbyname(shipment.containers, pattern, regex))


@doors.command('list')
@click.pass_obj
def list_doors(containers):
    """List all containers' doors with urls"""
    for container in containers:
        for name, door in container.doors.items():
            for url in door.urls:
                click.echo('{:30.30} {:20.20} {}'.format(container.fullname, name, url))


@utils.makesorted(lambda o: o.fullname)
def filterbyname(objects, pattern, regex):
    if not regex:
        pattern = fnmatch.translate(pattern)
    for obj in objects:
        if re.match(pattern, obj.fullname):
            yield obj


def getversion():
    try:
        return pkg_resources.get_distribution('dominator').version
    except pkg_resources.DistributionNotFound:
        return '(local)'
