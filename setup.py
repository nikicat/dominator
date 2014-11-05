import sys
import setuptools
from setuptools.command.test import test as TestCommand


class PyTest(TestCommand):
    user_options = [('pytest-args=', 'a', "Arguments to pass to py.test")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.pytest_args = None

    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = []
        self.test_suite = True

    def run_tests(self):
        # import here, cause outside the eggs aren't loaded
        import pytest
        errno = pytest.main(self.pytest_args)
        sys.exit(errno)


if __name__ == '__main__':
    setuptools.setup(
        name='dominator',
        version='13.0.1',
        url='https://github.com/yandex-sysmon/dominator',
        license='LGPLv3',
        author='Nikolay Bryskin',
        author_email='devel.niks@gmail.com',
        description='Cloud deployment toolbox',
        platforms='linux',
        packages=['dominator.entities', 'dominator.actions', 'dominator.utils'],
        namespace_packages=['dominator'],
        entry_points={'console_scripts': ['dominator = dominator.actions:cli']},
        package_data={
            'dominator.utils': ['*.yaml'],
            'dominator.actions': ['debian/*', 'debian/source/*']
        },
        exclude_package_data={'dominator.actions': ['debian/source']},
        classifiers=[
            'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
            'Operating System :: POSIX :: Linux',
            'Programming Language :: Python :: 3',
            'Topic :: Software Development :: Libraries :: Python Modules',
            'Topic :: System :: Distributed Computing',
        ],
        install_requires=[
            'docker-py >= 0.5.0',
            'mako',
            'colorama',
            'click',
            'mergedict',
        ],
        tests_require=[
            'pytest',
            'vcrpy',
        ],
        extras_require={
            'full': ['PyYAML.Yandex >= 3.11.1', 'colorlog', 'tzlocal', 'pkginfo', 'openssh_wrapper',
                     'objgraph', 'pyopenssl', 'psutil', 'vcrpy', 'requests>=2.4', 'urllib3>=1.9.1'],
            'tiny': ['PyYAML'],
        },
        cmdclass={'test': PyTest},
    )
