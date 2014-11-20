import logging
import logging.config
import os
import pkg_resources
import fnmatch
import re
import functools
import json
import sys
import importlib
import pickle

import yaml
import mako.template
from colorama import Fore
import click
import tabloid

from ..entities import SourceImage, BaseShip, BaseFile, Volume, Container, Shipment, LocalShip
from .. import utils


def getlogger():
    return utils.getcontext('logger')


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


def load_plugins():
    for plugin in utils.settings.get('plugins', []):
        importlib.import_module(plugin)


def ensure_shipment(shipment):
    if not isinstance(shipment, Shipment):
        raise InvalidShipmentFile("Shipment file should consist serialized Shipment object only")


@click.group()
@click.option('-s', '--shipment', type=click.Path(), default='./shipment.pickle', show_default=True,
              help="file to load shipment from/save shipment to")
@click.option('-c', '--config', type=click.File('r'), help="file path to load settings from")
@click.option('-l', '--loglevel', callback=validate_loglevel, default='warn')
@click.option('--vcr', type=click.Path(), help="mock all http requests with vcrpy and save cassete")
@click.option('-o', '--override', multiple=True, help="overide setting from config")
@click.version_option()
@click.pass_context
def cli(ctx, shipment, loglevel, config, vcr, override):
    logging.basicConfig(level=loglevel)
    logging.debug("dominator {} started".format(utils.getversion()))
    utils.settings.load(config)
    for option in override:
        assert re.match('[a-z\.\-]+=.*', option), "Options should have format <key=value>, not <{}>".format(option)
        key, value = option.split('=')
        utils.settings[key] = value
    default_logging_config = yaml.load(utils.resource_string('../utils/logging.yaml'))['logging']
    logging.config.dictConfig(utils.settings.get('logging', default_logging_config))
    logging.disable(level=loglevel-1)
    utils.setcontext(logger=logging.getLogger('dominator'))

    sys.excepthook = lambda *exc_info: getlogger().error("Unhandled exception occurred", exc_info=exc_info)

    load_plugins()

    if os.path.exists(shipment):
        try:
            utils.getlogger().debug("loading shipment", shipment_filename=shipment)
            with click.open_file(shipment, 'rb') as file:
                ctx.obj = pickle.load(file)
            ensure_shipment(ctx.obj)
            if ctx.obj.dominator_version != getshortversion():
                utils.getlogger().warning("current dominator version {} do not match shipment version {}".format(
                    getshortversion(), ctx.obj.dominator_version))
        except Exception:
            getlogger().exception("failed to load shipment")
            ctx.fail("Failed to load shipment")
    else:
        ctx.obj = Shipment()

    if vcr:
        import vcr as vcrpy
        cassette = vcrpy.use_cassette(vcr)
        cassette.__enter__()
        ctx.call_on_close(lambda: cassette.__exit__(None, None, None))


@cli.group()
def edit():
    """Commands to edit shipment."""


class InvalidShipmentFile(Exception):
    pass


def getshortversion():
    """This function returns short version independend on prerelease suffixes."""
    return '.'.join(utils.getversion().split('-')[0].split('.')[:3])


def edit_subcommand(name=None):
    def decorator(func):
        @edit.command(name=name)
        @click.pass_context
        @functools.wraps(func)
        def wrapper(ctx, *args, **kwargs):
            func(ctx, *args, **kwargs)
            try:
                filename = ctx.parent.parent.params['shipment']
                shipment = ctx.obj
                utils.getlogger().debug("saving shipment", shipment_filename=filename)
                shipment.dominator_version = getshortversion()

                data = pickle.dumps(shipment)
                with click.open_file(filename, 'bw+') as file:
                    file.write(data)
            except Exception as e:
                getlogger().exception("failed to save shipment")
                ctx.fail("Failed to save shipment: {!r}".format(e))
        return wrapper
    return decorator


@edit_subcommand()
def noop(_ctx):
    """Do nothing and just save the shipment."""


@edit_subcommand()
@click.argument('name')
def rename(ctx, name):
    """Rename shipment."""
    ctx.obj.name = name


@edit_subcommand()
def unload(ctx):
    """Unload all containers from ships."""
    ctx.obj.unload_ships()


@edit_subcommand()
@click.argument('distribution', required=False, metavar='<distribution>')
@click.argument('entrypoint', required=False, metavar='<entrypoint>')
@click.argument('arguments', nargs=-1, metavar='<arguments>')
def generate(ctx, distribution, entrypoint, arguments):
    """Generates yaml config file for shipment."""
    if distribution is None:
        click.echo('\n'.join([pkgname for pkgname in pkg_resources.Environment() if pkgname.startswith('obedient.')]))
        ctx.exit()

    dist = pkg_resources.get_distribution(distribution)
    assert dist is not None, "Could not load distribution for {}".format(distribution)

    if entrypoint is None:
        entrypoints = list(dist.get_entry_map('obedient').keys())
        if len(entrypoints) == 0:
            ctx.fail("Invalid obedient: no entrypoints")
        elif len(entrypoints) == 1:
            entrypoint = entrypoints[0]
        else:
            # Show all "obedient" entrypoints for package
            for entrypoint in entrypoints:
                click.echo(entrypoint)
            ctx.exit()

    getlogger().info("generating config", distribution=distribution, entrypoint=entrypoint)

    if entrypoint is None:
        entrypoint = list(dist.get_entry_map('obedient').keys())[0]
        getlogger().info("autodetected entrypoint is %s", entrypoint)

    func = dist.load_entry_point('obedient', entrypoint)
    assert func is not None, "Could not load entrypoint {} from distribution {}".format(entrypoint, distribution)
    execute_on_shipment(ctx.obj, func, arguments)


@edit_subcommand()
@click.argument('filename', type=click.Path(), metavar='<script.py>')
@click.argument('function', default='build', metavar='<function>')
@click.argument('arguments', nargs=-1, metavar='<arguments>')
def execute(ctx, filename, function, arguments):
    """Execute function from Python script."""
    assert filename.endswith('.py'), "Filename should be .py file"
    sys.path.append(os.path.dirname(filename))
    module = importlib.import_module(os.path.basename(filename[:-3]))
    function = getattr(module, function)
    execute_on_shipment(ctx.obj, function, arguments)


def execute_on_shipment(shipment, func, arguments):
    try:

        def parse_value(value):
            if value.isdigit():
                return int(value)
            if value[0] == '{':
                return json.loads(value)
            return value

        args = []
        kwargs = {}
        for arg in arguments:
            if '=' in arg:
                key, value = arg.split('=')
                kwargs[key] = parse_value(value)
            else:
                args.append(parse_value(arg))
        func(shipment, *args, **kwargs)
    except Exception:
        getlogger().exception('failed to generate obedient')
        raise

    getlogger().debug("retrieving image ids")
    for image in shipment.images:
        if not isinstance(image, SourceImage):
            with utils.addcontext(logger=logging.getLogger('dominator.image'), image=image):
                if image.getid() is None:
                    image.pull()
                    if image.getid() is None:
                        raise RuntimeError("Could not find id for image {}".format(image))


@cli.group(chain=True)
def shipment():
    """Shipment management commands."""
    utils.setcontext(logger=logging.getLogger('dominator.shipment'))


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
@click.argument('filename', type=click.Path())
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


def add_filtering(func):
    @functools.wraps(func)
    @click.option('-p', '--pattern', default='*', help="pattern to filter objects by name")
    @click.option('-r', '--regex', is_flag=True, default=False, help="use regexp instead of wildcard")
    @click.option('-i', '--interactive', is_flag=True, default=False, help="interactive filtering")
    def wrapper(*args, pattern, regex, interactive, **kwargs):

        @utils.makesorted(lambda o: o.fullname)
        def filterbyname(objects, pattern, regex):
            if not regex:
                pattern = fnmatch.translate(pattern)
            for obj in objects:
                if re.match(pattern, obj.fullname):
                    yield obj

        def filterobjects(objects, pattern, regex, interactive):
            objects = filterbyname(objects, pattern, regex)
            if interactive:
                choices = sorted([' {:2}: {}'.format(i, obj.fullname) for i, obj in enumerate(objects, 1)])
                resp = click.prompt('Select objects:\n' + '\n'.join(choices) + '\nEnter choice (1,2-5 or all)')
                if resp == 'all':
                    yield from objects
                else:
                    try:
                        def parse_indexes(resp):
                            for index in resp.split(','):
                                if '-' in index:
                                    start, end = index.split('-')
                                    yield from range(int(start), int(end)+1)
                                else:
                                    yield int(index)
                        indexes = list(parse_indexes(resp))
                    except:
                        raise RuntimeError("Invalid input (should be a number)")
                    for i, obj in enumerate(objects, 1):
                        if i in indexes:
                            yield obj
            else:
                yield from objects

        return func(*args, filter=lambda objects: filterobjects(objects, pattern, regex, interactive), **kwargs)
    return wrapper


def print_table(columns):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            table = tabloid.FormattedTable()
            for column in columns:
                table.add_column(column)
            for row in func(*args, **kwargs):
                table.add_row(row)
            click.echo('\n'.join(table.get_table()))
        return wrapper
    return decorator


def green(text):
    return Fore.GREEN + text + Fore.RESET


def red(text):
    return Fore.RED + text + Fore.RESET


def yellow(text):
    return Fore.YELLOW + text + Fore.RESET


@cli.group(chain=True)
@click.pass_context
@add_filtering
def container(ctx, filter):
    """Container management commands."""
    ctx.obj = filter(ctx.obj.containers)


def foreach(varname):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(objects, *args, **kwargs):
            with utils.addcontext(logger=logging.getLogger('dominator.'+varname)):
                for obj in objects:
                    with utils.addcontext(**{varname: obj}):
                        try:
                            func(obj, *args, **kwargs)
                        except Exception:
                            getlogger().exception('error while executing {} on {}'.format(func.__name__, obj))
                            sys.exit(1)
        return wrapper
    return decorator


@container.command()
@click.pass_obj
@foreach('container')
def start(cont):
    """Push images, render config volumes and Start containers."""
    cont.run()


@container.command()
@click.pass_obj
@foreach('container')
def restart(cont):
    """Restart containers."""
    cont.check()
    if cont.running:
        cont.stop()
    cont.run()


@container.command('exec')
@click.pass_obj
@click.option('-k', '--keep', is_flag=True, default=False, help="keep container after stop")
@click.argument('command')
@foreach('container')
def container_exec(container, command, keep):
    """Start, attach and wait a container."""
    if command is not None:
        container.command = command
    common_exec(container, keep)


def common_exec(cont, keep):
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
@foreach('container')
def stop(cont):
    """Stop container(s) on ship(s)."""
    cont.check()
    if cont.running:
        cont.stop()


@container.command()
@click.pass_obj
@click.option('-f', '--force', is_flag=True, default=False, help="Kill and remove container")
@foreach('container')
def remove(cont, force):
    """Remove container(s) on ship(s)."""
    cont.check()
    if cont.id is None:
        utils.getlogger().info("skipping container as it doesn't exist")
    else:
        cont.remove(force)


@container.command('list')
@click.pass_obj
@foreach('container')
def container_list(container):
    """Print container names."""
    click.echo(container.fullname)


@container.command()
@click.pass_obj
@click.option('-d', '--showdiff', is_flag=True, default=False, help="show diff with running container")
def status(containers, showdiff):
    """Show container status."""
    with utils.addcontext(logger=logging.getLogger('dominator.container')):
        status = 0
        table = tabloid.FormattedTable()
        for column in ['name', 'id', 'status']:
            table.add_column(column)
        diffs = {}
        for i, container in enumerate(containers):
            with utils.addcontext(container=container):
                container.check()
                if container.running:
                    diff = list(utils.compare_container(container, container.inspect()))
                    if len(diff) > 0:
                        color = Fore.YELLOW
                        if showdiff:
                            diffs[i] = '\n'.join(format_diff(diff))
                        status = 2
                    else:
                        color = Fore.GREEN
                else:
                    color = Fore.RED
                    status = 1
                table.add_row([container.fullname, color+(container.id or '')[:7], container.status+Fore.RESET])

        header, rows = table.get_table()
        click.echo(header)
        for i, row in enumerate(rows.split('\n')):
            click.echo(row)
            if i in diffs:
                click.echo(diffs[i])

        sys.exit(status)


def format_diff(difflist):
    fore = Fore
    for key, diff in difflist:
        keystr = ' '.join(key)
        if isinstance(diff, list):
            # files diff
            for line in diff:
                color = {'- ': Fore.RED, '+ ': Fore.GREEN, '? ': Fore.BLUE}.get(line[:2], '')
                yield '  {keystr:60.60} {color}{line}{fore.RESET}'.format(**locals())
        elif len(diff) == 2:
            expected, actual = diff
            yield ('  {keystr:60.60} {fore.RED}{actual!s:50.50}{fore.RESET} '
                   '{fore.GREEN}{expected!s:50.50}{fore.RESET}').format(**locals())
        else:
            raise ValueError("Invalid diff format for {key}: {diff}".format(**locals()))


@container.command()
@click.pass_obj
@click.option('-f', '--follow', is_flag=True, default=False, help="follow logs")
@foreach('container')
def log(cont, follow):
    """View Docker log for container(s)."""
    cont.check()
    for line in cont.logs(follow=follow):
        click.echo(line)


@container.command('dump')
@click.pass_obj
@foreach('container')
def dump_container(container):
    """Dump container info."""
    container.ship = None
    container.shipment = None
    for volume in container.volumes.values():
        if hasattr(volume, 'files'):
            for file in volume.files.values():
                if hasattr(file, 'context'):
                    file.context = 'skipped'
    click.echo_via_pager(yaml.dump(container))


@container.command()
@click.pass_obj
@click.argument('command', default='bash -i')
@foreach('container')
def enter(cont, command):
    """nsenter to container."""
    cont.check()
    cont.enter(command)


@cli.group(chain=True)
@click.pass_context
@add_filtering
def task(ctx, filter):
    """Container management commands."""
    ctx.obj = filter(ctx.obj.tasks.values())


@task.command('exec')
@click.pass_obj
@click.argument('command')
@foreach('task')
def task_exec(task, command):
    """Execute task. <command> could be used to override default command."""
    if command is not None:
        task.command = command
    if task.ship is None:
        ship = LocalShip()
        ship.place(task)
        ship.shipment = task.shipment
    task.exec_with_tty()


@task.command('list')
@click.pass_obj
@foreach('task')
def task_list(task):
    """Print task names."""
    click.echo(task.fullname)


@cli.group()
@add_filtering
@click.pass_context
def volume(ctx, filter):
    """Commands to manage volumes."""
    ctx.obj = filter(ctx.obj.volumes)


@volume.command('list')
@click.pass_obj
@foreach('volume')
def list_volumes(volume):
    """List volumes."""
    click.echo('{volume.fullname:30.30} {volume.dest:30.30} {volume.fullpath}'.format(volume=volume))


@volume.command('erase')
@click.option('-y', '--yes', is_flag=True, default=False, help="skip confirmation")
@click.pass_obj
@foreach('volume')
def erase_volume(volume, yes):
    """Eraes all volume data (dangerous!)."""
    if not yes:
        if not click.confirm("Delete all the data from {volume.fullpath} "
                             "on {volume.container.ship.name}?".format(volume=volume)):
            return
    volume.erase()


@cli.group()
@add_filtering
@click.pass_context
def file(ctx, filter):
    """File management commands."""
    ctx.obj = filter(ctx.obj.files)


@file.command('list')
@click.pass_obj
@foreach('file')
def list_files(file):
    """List files."""
    click.echo('{file.fullname:60.60} {file.fullpath}'.format(file=file))


@file.command('view')
@click.pass_obj
@foreach('file')
def view_files(file):
    """View file via `less'."""
    file.volume.container.ship.spawn('less -S {}'.format(file.fullpath))


@cli.group(chain=True)
@click.pass_context
@add_filtering
def image(ctx, filter):
    """Image management commands."""
    ctx.obj = filter([image for image in ctx.obj.images if isinstance(image, SourceImage)])


@image.command()
@click.pass_obj
@click.option('-n', '--nocache', is_flag=True, default=False, help="disable Docker cache")
@click.option('-r', '--rebuild', is_flag=True, default=False, help="rebuild image even if alredy built (hashtag found)")
@foreach('image')
def build(image, nocache, rebuild):
    """Build source images."""
    # image.getid() == None means that image with given tag doesn't exist
    if rebuild or image.getid() is None:
        image.build(nocache=nocache)


@image.command()
@click.pass_obj
@foreach('image')
def push(image):
    """Push images to Docker registry."""
    image.push()


@image.command('list')
@click.pass_obj
@foreach('image')
def list_images(image):
    """Print image list in build order."""
    click.echo(image.repository)


@cli.group()
@click.pass_context
@add_filtering
def ship(ctx, filter):
    """Ship management commands."""
    ctx.obj = filter(ctx.obj.ships.values())


@ship.command('list')
@click.pass_obj
@foreach('ship')
def list_ships(ship):
    """List ships in format "<name>      <fqdn>"."""
    click.echo('{:15.15}{}'.format(ship.name, ship.fqdn))


@ship.command('status')
@click.pass_obj
@print_table(['name', 'url', 'version', 'api', 'status'])
def status_ships(ships):
    """Output ship status."""
    for ship in ships:
        try:
            ship.docker.ping()
        except Exception as e:
            status = red(str(e))
            version = {'Version': yellow('unknown'), 'ApiVersion': yellow('unknown')}
        else:
            status = green('ok')
            version = ship.docker.version()
        yield ship.fullname, ship.url, version['Version'], version['ApiVersion'], status


@ship.command('restart')
@click.pass_obj
@foreach('ship')
def restart_ship(ship):
    """Restart ship(s)."""
    ship.restart()


@ship.command('exec')
@click.pass_obj
@click.argument('command')
@foreach('ship')
def execute_command_on_ship(ship, command):
    """Execute command on ship(s)."""
    ship.spawn(command)


@ship.command('info')
@click.pass_obj
@foreach('ship')
def ship_info(ship):
    """Show Docker info."""
    click.echo(yaml.dump(ship.info(), default_flow_style=False))


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
        for cinfo in ship.docker.containers(all=True):
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
@click.pass_context
@add_filtering
def door(ctx, filter):
    """Commands to view doors."""
    ctx.obj = filter(ctx.obj.doors)


@door.command('list')
@click.pass_obj
def list_doors(doors):
    """List all containers' doors with urls"""
    for door in doors:
        click.echo('{door.fullname:40.40} {door.port:5}'.format(door=door))


@door.command('test')
@click.pass_obj
@print_table(['name', 'port', 'status'])
def test_doors(doors):
    for door in doors:
        try:
            door.test()
        except Exception as e:
            result = red(str(e))
        else:
            result = green('ok')
        yield door.fullname, door.hostport, result


@cli.group()
@click.pass_context
@add_filtering
def url(ctx, filter):
    """Commands to view urls."""
    ctx.obj = filter(ctx.obj.urls)


@url.command('list')
@click.pass_obj
def list_urls(urls):
    for url in urls:
        click.echo('{url.fullname:50.50} {url}'.format(url=url))


@url.command('test')
@click.pass_obj
@click.option('-t', '--timeout', default=2, help="Timeout in seconds")
@print_table(['name', 'url', 'status'])
def test_urls(urls, timeout):
    for url in urls:
        try:
            result = green(url.test(timeout) or 'ok')
        except NotImplementedError:
            result = yellow('not implemented')
        except Exception as e:
            result = red(str(e))
        yield url.fullname, str(url), result


@cli.group()
def config():
    """Commands to manage local config files."""
    utils.setcontext(logger=logging.getLogger('dominator.config'))


@config.command('dump')
def dump_config():
    """Dump loaded configuration in YAML format."""
    click.echo(yaml.dump(utils.settings.get('', default={})))


@config.command('create')
def create_config():
    """(Re)create config files with default values."""
    for filename in ['settings.yaml', 'logging.yaml']:
        src = pkg_resources.resource_stream('dominator.utils', filename)
        dstpath = os.path.join(utils.settings.dirpath, filename)
        if os.path.exists(dstpath):
            if not click.confirm("File {} exists. Are you sure you want to overwrite it?".format(dstpath)):
                continue
        getlogger().debug("writing config to {}".format(dstpath))
        with open(dstpath, 'wb+') as dst:
            dst.write(src.read())


@edit_subcommand('local-ship')
def add_local_ship(ctx):
    """Populate shipment with one local ship."""
    ctx.obj.ships['local'] = LocalShip()


@cli.command()
@click.pass_obj
def export(shipment):
    """Export shipment in YAML format."""
    click.echo(yaml.dump(shipment, default_flow_style=False))


@edit_subcommand('import')
@click.pass_context
def import_shipment(ctx):
    """Import shipment from YAML format."""
    shipment = yaml.load(sys.stdin)
    ensure_shipment(shipment)
    ctx.obj = shipment
