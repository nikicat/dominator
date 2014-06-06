import logging
import os
import sys
import importlib
import yaml
import argh

_logger = logging.getLogger(__name__)
input_arg = argh.arg('input', help='input config file path (*.py:func or *.yaml)')


def literal_str_representer(dumper, data):
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|' if '\n' in data else None)


@input_arg
def dump(input: str):
    yaml.add_representer(str, literal_str_representer)
    print(yaml.dump(load(input)))


@input_arg
@argh.arg('cname', help='container name to run')
@argh.arg('--remove', action='store_true', help='remove container after stop')
@argh.arg('--pull', action='store_true', help='pull repository before start')
def run(input: str, cname: str, remove: bool=False, pull: bool=False):
    for container in load(input):
        if container.ship.islocal and container.name == cname:
            run_container(container, remove=remove, pull=pull)
            break
    else:
        raise RuntimeError('could not find container')


def containers_by_name(dock, name, **kwargs):
    return list([cont for cont in dock.containers(**kwargs) if cont['Names'][0][1:] == name])


def run_container(cont, remove, pull):
    import docker

    for volume in cont.volumes:
        volume.prepare(cont)

    _logger.info('connecting to local docker')
    dock = docker.Client()

    if pull:
        _logger.info('pulling image')
        dock.pull(cont.repository, cont.tag)

    running = containers_by_name(dock, cont.name)
    if len(running) > 0:
        _logger.info('found running container with the same name, stopping')
        dock.stop(running[0])

    stopped = containers_by_name(dock, cont.name, all=True)
    if len(stopped):
        _logger.info('found stopped container with the same name, removing')
        dock.remove_container(stopped[0])

    _logger.info('creating container')
    cont_info = dock.create_container(
        image='{}:{}'.format(cont.repository, cont.tag),
        hostname='{}-{}'.format(cont.name, cont.ship.name),
        mem_limit=cont.memory,
        environment=cont.env,
        name=cont.name,
        ports=list(cont.ports.values()),
    )

    _logger.info('starting container %s', cont_info['Id'])
    dock.start(
        cont_info,
        port_bindings={'{}/tcp'.format(v): ('::', v) for v in cont.ports.values()},
        binds={v.getpath(cont): {'bind': v.dest, 'ro': v.ro} for v in cont.volumes},
    )

    _logger.info('attaching to container %s', cont_info['Id'])
    try:
        logger = logging.getLogger(cont.name)
        for line in dock.logs(cont_info, stream=True):
            logger.info(line.decode()[:-1])
    except KeyboardInterrupt:
        _logger.info('received keyboard interrupt')

    _logger.info('stopping container')
    dock.stop(cont_info, timeout=2)

    if remove:
        _logger.info('removing container')
        dock.remove_container(cont_info)


@input_arg
def containers(input: str):
    for container in load(input):
        if container.ship.islocal:
            print(container.name)


def load(filename):
    _logger.info('loading from %s', filename)
    if ':' in filename:
        filename, func = filename.split(':')
    else:
        func = 'main'
    if filename.endswith('.py'):
        sys.path.append(os.path.dirname(filename))
        module = importlib.import_module(os.path.basename(filename)[:-3])
        return getattr(module, func)()
    elif filename.endswith('.yaml'):
        with open(filename) as f:
            return yaml.load(f)


def makedeb():
    pass
