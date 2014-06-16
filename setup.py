import sys
import setuptools
from setuptools.command.test import test as TestCommand


class Tox(TestCommand):
    user_options = [('tox-args=', 'a', "Arguments to pass to tox")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.tox_args = None

    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = []
        self.test_suite = True

    def run_tests(self):
        import tox
        import shlex
        errno = tox.cmdline(args=shlex.split(self.tox_args))
        sys.exit(errno)


if __name__ == '__main__':
    setuptools.setup(
        name='dominator',
        version='0.1',
        url='https://github.com/nikicat/dominator',
        license='GPLv3',
        author='Nikolay Bryskin',
        author_email='devel.niks@gmail.com',
        description='Cloud deployment toolbox',
        platforms='linux',
        packages=['dominator'],
        entry_points={'console_scripts': ['dominator = dominator:main']},
        package_data={'dominator': ['settings.yaml']},
        classifiers=[
            'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
            'Operating System :: POSIX :: Linux',
            'Programming Language :: Python :: 3',
            'Topic :: Software Development :: Libraries :: Python Modules',
            'Topic :: System :: Distributed Computing',
        ],
        install_requires=[
            'docker-py',
            'docopt',
            'pyyaml',
            'mako',
            'structlog',
        ],
        install_recommends=[
            'requests',
            'python_novaclient',
            'conductor_client',
            'psutil',
            'colorama',
            'pyquery',
        ],
        tests_require=[
            'tox',
            'pytest',
            'pytest-cov',
        ],
        cmdclass={'test': Tox},
    )
