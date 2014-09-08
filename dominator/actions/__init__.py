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

from ..entities import SourceImage
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
        ctx.obj = yaml.load(config)


@cli.group(chain=True)
def shipment():
    pass


@shipment.command()
@click.argument('distribution', metavar='<distribution>')
@click.argument('entrypoint', metavar='<entrypoint>')
@click.option('--cache/--no-cache', default=True)
@click.option('--clear-cache', is_flag=True, default=False, help="clear requests_cache before run (requires --cache)")
def generate(distribution, entrypoint, cache, clear_cache):
    """Generates yaml config file for shipment obtained as result of invoking
    <entrypoint> from <distribution>
    """
    getlogger().info("generating config", distribution=distribution, entrypoint=entrypoint)

    dist = pkg_resources.get_distribution(distribution)
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
        if clear_cache:
            requests_cache.clear()
        with requests_cache.enabled():
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

    click.echo_via_pager(yaml.dump(shipment))


@shipment.command()
@click.pass_obj
@click.argument('packagename')
@click.argument('distribution', default='unstable')
@click.argument('urgency', default='low')
@click.option('-t', '--target', type=click.Path(), default='./', help="target directory to create debian/ inside")
def makedeb(shipment, packagename, distribution, urgency, target):
    """Create debian/ directory in target dir ready for building debian package
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
                    distribution=distribution,
                    urgency=urgency,
                )
                with open(os.path.join(target, path), 'w+') as output:
                    output.write(rendered)

    render_dir('debian')

    with open(os.path.join(target, 'debian', '{}.yaml'.format(packagename)), 'w+') as config:
        yaml.dump(shipment, config)


@cli.group()
@click.pass_context
@click.option('-s', '--ship', 'shippattern', default='*', help="pattern to filter ships")
@click.option('-c', '--container', 'containerpattern', default='*', help="pattern to filter containers")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
def containers(ctx, shippattern, containerpattern, regex):
    shipment = ctx.obj
    if not regex:
        shippattern, containerpattern = map(fnmatch.translate, (shippattern, containerpattern))

    containers = []

    for ship in shipment.ships:
        if re.match(shippattern, ship.name):
            for container in ship.containers:
                if re.match(containerpattern, container.name):
                    containers.append(container)

    ctx.obj = containers


@containers.command()
@click.pass_obj
def start(containers):
    """Push images, render config volumes and Start containers
    """
    for cont in containers:
        cont.run()


@containers.command()
@click.pass_obj
def restart(containers):
    """Restart containers
    """
    for cont in containers:
        cont.check()
        if cont.running:
            cont.stop()
        cont.start()


@containers.command()
@click.pass_obj
@click.option('-k', '--keep', is_flag=True, default=False, help="keep container after stop")
def exec(containers, keep):
    """Start container, attach to process, read stdout/stderr
    and print it, then (optionally) remove it
    """
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


@containers.command()
@click.pass_obj
def stop(containers):
    """Stop container(s) on ship(s)
    """
    for cont in containers:
        cont.check()
        if cont.running:
            cont.stop()


@containers.command('list')
@click.pass_obj
def list_containers(containers):
    """Print container names
    """
    for container in containers:
        click.echo(container.name)


@containers.command()
@click.pass_obj
@click.option('-d', '--showdiff', is_flag=True, default=False, help="show diff with running container")
def status(containers, showdiff):
    """Show shipment's status
    """
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
        click.echo('{c.shipment.name:20.20} {c.ship.name:10.10} {c.name:20.20} '
                   '{color}{id:10.7} {c.status:30.30}{reset}'
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


@containers.command()
@click.pass_obj
@click.option('-f', '--follow', is_flag=True, default=False, help="follow logs")
def log(containers, follow):
    """Fetch logs for containers
    """
    for cont in containers:
        cont.check()
        cont.logs(follow=follow)


@containers.command('dump')
@click.pass_obj
def containers_dump(containers):
    """Dump container info"""
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
@click.pass_context
@click.option('-p', '--pattern', 'pattern', default='*', help="pattern to filter images")
@click.option('-r', '--regex', is_flag=True, default=False, help="use regex instead of wildcard")
def images(ctx, pattern, regex):
    shipment = ctx.obj
    images = []
    if not regex:
        pattern = fnmatch.translate(pattern)
    for image in shipment.images:
        if re.match(pattern, image.repository):
            images.append(image)
    ctx.obj = images


@images.command()
@click.pass_obj
@click.option('-n', '--nocache', is_flag=True, default=False, help="disable Docker cache")
@click.option('-p', '--push', is_flag=True, default=False, help="push image to registry after the build")
@click.option('-r', '--rebuild', is_flag=True, default=False, help="rebuild image even if alredy built (hashtag found)")
def build(images, nocache, push, rebuild):
    """Build source images
    """
    for image in images:
        if not isinstance(image, SourceImage):
            continue
        # image.getid() == None means that image with given tag doesn't exist
        if rebuild or image.getid() is None:
            image.build(nocache=nocache)
        if push:
            image.push()


@images.command('list')
@click.pass_obj
def list_images(images):
    """Print image list in build order
    """
    for image in images:
        click.echo(image)


def getversion():
    try:
        return pkg_resources.get_distribution('dominator').version
    except pkg_resources.DistributionNotFound:
        return '(local)'
