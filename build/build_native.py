#!/usr/bin/env python

disclaimer="""
!!! This script is not a part of public CatBoost API yet and subject to change without notice. !!!
"""

import argparse
import copy
import logging
import os
import platform
import subprocess
import sys
import tempfile

if sys.version_info < (3, 8):
    import pipes
else:
    import shlex



class Target(object):
    def __init__(self, catboost_component, need_pic):
        self.catboost_component = catboost_component
        self.need_pic = need_pic

class Targets(object):
    catboost = {
        'catboost': Target('app', need_pic=False),
        'catboostmodel_static': Target('libs', need_pic=False),
        '_hnsw': Target('python-package', need_pic=True),
        '_catboost': Target('python-package', need_pic=True),
        'catboostr': Target('R-package', need_pic=True),
        'catboostmodel': Target('libs', need_pic=True),
        'catboost_train_interface': Target('libs', need_pic=True),
        'catboost4j-prediction': Target('jvm-packages', need_pic=True),
        'catboost4j-spark-impl': Target('spark', need_pic=True),
        'catboost4j-spark-impl-cpp': Target('spark', need_pic=True)
    }
    tools = [
        'archiver',
        'cpp_styleguide',
        'enum_parser',
        'flatc',
        'protoc',
        'rescompiler',
        'triecompiler'
    ]


class Option(object):
    def __init__(self, description, required=False, default=None, opt_type=str):
        self.description = description
        self.required = required
        if required and (default is not None):
            raise RuntimeError("Required option shouldn't have default specified")
        self.default = default
        self.opt_type = opt_type


class Opts(object):
    known_opts = {
        'dry_run': Option('Only print, not execute commands', default=False, opt_type=bool),
        'verbose': Option('Verbose output for CMake and Ninja', default=False, opt_type=bool),
        'build_root_dir': Option('CMake build dir (-B)', required=True),
        'build_type': Option('build type (Debug,Release,RelWithDebInfo,MinSizeRel)', default='Release'),
        'targets':
            Option(
                f'List of CMake targets to build (,-separated). Note: you cannot mix targets that require PIC and non-PIC targets here',
                required=True,
                opt_type=list
            ),
        'cmake_build_toolchain': Option(
            'Custom CMake toolchain path for building CatBoost tools instead of one of preselected'
            + ' (used only in cross-compilation)'
        ),
        'cmake_target_toolchain': Option(
            'Custom CMake toolchain path for target platform instead of one of preselected\n'
            + ' (used even in default case when no explicit target_platform is specified)'
        ),
        'conan_host_profile': Option('Custom Conan host profile instead of one of preselected (used only in cross-build)'),
        'msvs_installation_path':
            Option('Microsoft Visual Studio installation path (default is "{Program Files}\\Microsoft Visual Studio\\")'
        ),
        'msvs_version': Option('Microsoft Visual Studio version (like "2019", "2022")', default='2019'),
        'msvc_toolset': Option('Microsoft Visual C++ Toolset version to use', default='14.28.29333'),
        'macosx_version_min': Option('Minimal macOS version to target', default='11.0'),
        'have_cuda': Option('Enable CUDA support', default=False, opt_type=bool),
        'cuda_root_dir': Option('CUDA root dir (taken from CUDA_PATH or CUDA_ROOT by default)'),
        'cuda_runtime_library': Option('CMAKE_CUDA_RUNTIME_LIBRARY for CMake', default='Static'),
        'android_ndk_root_dir': Option('Path to Android NDK root'),
        'cmake_extra_args': Option(
            'Extra args for CMake (,-separated), \n'
            + 'in case of cross-compilation used only for target platform, not for building tools',
            default=[],
            opt_type=list
        ),
        'target_platform':
            Option(
                'Target platform to build for (like "linux-aarch64"), same as host platform by default'
            ),
        'native_built_tools_root_dir':
            Option('Path to tools from CatBoost repository built for build platform (useful only for cross-compilation)')
    }

    def __init__(self, **kwargs):
        for key in kwargs:
            if key not in Opts.known_opts:
                raise RuntimeError(f'Unknown parameter for Opts: {key}')
            setattr(self, key, kwargs[key])
        for key, option in Opts.known_opts.items():
            if not hasattr(self, key):
                if option.required:
                    raise RuntimeError(f'Required option {key} not specified')
                setattr(self, key, option.default)


def get_host_platform():
    arch = platform.machine()
    if arch == 'AMD64':
        arch = 'x86_64'
    return f'{platform.system().lower()}-{arch}'

class CmdRunner(object):
    def __init__(self, dry_run=False):
        self.dry_run = dry_run

    @staticmethod
    def shlex_join(cmd):
        if sys.version_info >= (3, 8):
            return shlex.join(cmd)
        else:
            return ' '.join(
                pipes.quote(part)
                for part in cmd
            )

    def run(self, cmd, run_even_with_dry_run=False, **subprocess_run_kwargs):
        if 'shell' in subprocess_run_kwargs:
            printed_cmd = cmd
        else:
            printed_cmd = CmdRunner.shlex_join(cmd)
        logging.info(f'Running "{printed_cmd}"')
        if run_even_with_dry_run or (not self.dry_run):
            subprocess.run(cmd, check=True, **subprocess_run_kwargs)

def get_source_root_dir():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def get_default_cross_build_toolchain(source_root_dir, opts):
    build_system_name = platform.system().lower()
    target_system_name, target_system_arch = opts.target_platform.split('-')

    default_toolchains_dir = os.path.join(source_root_dir, 'build','toolchains')

    if build_system_name == 'darwin':
        if target_system_name != 'darwin':
            raise RuntimeError('Cross-compilation to darwin from non-darwin is not supported')
        return os.path.join(
            default_toolchains_dir,
            f'cross-build.host.darwin.target.{target_system_arch}-darwin-default.clang.toolchain'
        )
    elif build_system_name == 'linux':
        if target_system_name == 'android':
            # use toolchain from NDK
            if opts.android_ndk_root_dir is None:
                raise RuntimeError('Android NDK root dir not specified')
            return os.path.join(opts.android_ndk_root_dir, 'build', 'cmake', 'android.toolchain.cmake')
        elif target_system_name == 'linux':
            return os.path.join(
                default_toolchains_dir,
                f'cross-build.host.linux.target.{target_system_arch}-linux-gnu.clang.toolchain'
            )
        else:
            raise RuntimeError(f'Cross-compilation from {build_system_name} to {target_system_name} is not supported')
    else:
        raise RuntimeError(f'Cross-compilation from {build_system_name} is not supported')

def get_default_conan_host_profile(source_root_dir, target_platform):
    target_system_name, target_system_arch = target_platform.split('-')

    # a bit of name mismatch
    conan_profile_target_system_name = 'macos' if target_system_name == 'darwin' else target_system_name
    return os.path.join(
        source_root_dir,
        'cmake',
        'conan-profiles',
        f'{conan_profile_target_system_name}.{target_system_arch}.profile'
    )


def cross_build(opts: Opts, cmd_runner=None):
    if cmd_runner is None:
        cmd_runner = CmdRunner(opts.dry_run)

    host_platform = get_host_platform()
    logging.info(f"Cross-building from host platform {host_platform} to target platform {opts.target_platform}")

    tmp_native_built_tools_root_dir = None

    if opts.native_built_tools_root_dir:
        native_built_tools_root_dir = opts.native_built_tools_root_dir
    else:
        tmp_native_built_tools_root_dir = tempfile.TemporaryDirectory(prefix='native_built_tools_root_dir')
        native_built_tools_root_dir = tmp_native_built_tools_root_dir.name

        logging.info("Build tools")
        build(
            dry_run=opts.dry_run,
            verbose=opts.verbose,
            build_root_dir=native_built_tools_root_dir,
            build_type='Release',
            targets=Targets.tools,
            cmake_target_toolchain=opts.cmake_build_toolchain,
            msvs_version=opts.msvs_version,
            msvc_toolset=opts.msvc_toolset,
            macosx_version_min=opts.macosx_version_min
        )

    source_root_dir = get_source_root_dir()

    if opts.cmake_target_toolchain is None:
        cmake_target_toolchain = get_default_cross_build_toolchain(source_root_dir, opts)
    else:
        cmake_target_toolchain = opts.cmake_target_toolchain

    if opts.conan_host_profile is None:
        conan_host_profile = get_default_conan_host_profile(source_root_dir, opts.target_platform)
    else:
        conan_host_profile = opts.conan_host_profile

    conan_cmd_prefix = [
        'conan',
        'install',
        '-s', f'build_type={opts.build_type}',
        '-if', opts.build_root_dir,
        '--build=missing',
    ]

    conan_tools_cmd = conan_cmd_prefix + [
        os.path.join(source_root_dir, 'conanfile.txt')
    ]
    logging.info("Run conan to build tools")
    cmd_runner.run(conan_tools_cmd)

    conan_libs_cmd = conan_cmd_prefix + [
        '--no-imports',
        f'-pr:h={conan_host_profile}',
        '-pr:b=default',
        os.path.join(source_root_dir, 'conanfile.txt')
    ]
    logging.info(f"Run conan to build libs for target platform {opts.target_platform}")
    cmd_runner.run(conan_libs_cmd)

    final_build_opts = copy.copy(opts)
    final_build_opts.cmake_target_toolchain = cmake_target_toolchain
    final_build_opts.native_built_tools_root_dir = native_built_tools_root_dir

    logging.info(f"Run build for target platform {opts.target_platform}")
    build(opts=final_build_opts, cross_build_final_stage=True)


def get_require_pic(targets):
    # will be set to defined value below
    first_target_with_no_pic=None
    first_target_with_pic=None

    for target in targets:
        if target in Targets.catboost:
            if Targets.catboost[target].need_pic:
                if first_target_with_no_pic is not None:
                    raise Exception(f'mixing targets that require PIC: {target} and not: {first_target_with_no_pic}')
                elif first_target_with_pic is None:
                    first_target_with_pic = target
            elif first_target_with_pic is not None:
                raise Exception(f'mixing targets that require PIC: {first_target_with_pic} and not: {target}')
        elif first_target_with_no_pic is None:
            first_target_with_no_pic = target

    return first_target_with_pic is not None

def get_msvc_environ(msvs_installation_path, msvs_version, msvc_toolset, cmd_runner, dry_run):
    if msvs_installation_path is None:
        program_files = 'Program Files' if msvs_version == '2022' else 'Program Files (x86)'
        msvs_base_dir = f'c:\\{program_files}\\Microsoft Visual Studio\\{msvs_version}'
    else:
        msvs_base_dir = os.path.join(msvs_installation_path, msvs_version)

    if os.path.exists(f'{msvs_base_dir}\\Community'):
        msvs_dir = f'{msvs_base_dir}\\Community'
    elif os.path.exists(f'{msvs_base_dir}\\Enterprise'):
        msvs_dir = f'{msvs_base_dir}\\Enterprise'
    else:
        raise RuntimeError(f'Microsoft Visual Studio {msvs_version} installation not found')

    env_vars = {}

    # can't use NamedTemporaryFile or mkstemp because of child proces access issues
    with tempfile.TemporaryDirectory() as tmp_dir:
        env_vars_file_path = os.path.join(tmp_dir, 'env_vars')
        cmd = f'"{msvs_dir}\\VC\\Auxiliary\\Build\\vcvars64.bat" -vcvars_ver={msvc_toolset} && set > {env_vars_file_path}'
        cmd_runner.run(cmd, run_even_with_dry_run=True, shell=True)
        with open(env_vars_file_path) as env_vars_file:
            for l in env_vars_file:
                key, value = l[:-1].split('=')
                env_vars[key] = value

    return env_vars

def get_cuda_root_dir(cuda_root_dir_option):
    cuda_root_dir = cuda_root_dir_option or os.environ.get('CUDA_PATH') or os.environ.get('CUDA_ROOT')
    if not cuda_root_dir:
        raise RuntimeError('No cuda_root_dir specified and CUDA_PATH and CUDA_ROOT environment variables also not defined')
    return cuda_root_dir

def add_cuda_bin_path_to_system_path(build_environ, cuda_root_dir):
    cuda_bin_dir = os.path.join(cuda_root_dir, 'bin')
    if platform.system().lower() == 'windows':
        build_environ['Path'] = cuda_bin_dir + ';' + build_environ['Path']
    else:
        build_environ['PATH'] = cuda_bin_dir + ':' + build_environ['PATH']

def get_catboost_components(targets):
    catboost_components = set()

    for target in targets:
        if target in Targets.catboost:
            catboost_components.add(Targets.catboost[target].catboost_component)

    return catboost_components

def get_default_build_platform_toolchain(source_root_dir):
    if platform.system().lower() == 'windows':
        return os.path.abspath(os.path.join(source_root_dir, 'build', 'toolchains', 'default.toolchain'))
    else:
        return os.path.abspath(os.path.join(source_root_dir, 'build', 'toolchains', 'clang.toolchain'))


def build(opts=None, cross_build_final_stage=False, **kwargs):
    if opts is None:
        opts = Opts(**kwargs)

    cmd_runner = CmdRunner(opts.dry_run)

    if opts.target_platform is not None:
        if (opts.target_platform != get_host_platform()) and not cross_build_final_stage:
            cross_build(opts, cmd_runner)
            return
        target_platform = opts.target_platform
    else:
        target_platform = get_host_platform()

    require_pic = get_require_pic(opts.targets)

    logging.info(
        f'target_platform={target_platform}. Building targets {" ".join(opts.targets)} {"with" if require_pic else "without"} PIC'
    )

    source_root_dir = get_source_root_dir()

    if opts.cmake_target_toolchain is None:
        cmake_target_toolchain = get_default_build_platform_toolchain(source_root_dir)
    else:
        cmake_target_toolchain = opts.cmake_target_toolchain

    if platform.system().lower() == 'windows':
        # Need vcvars set up for Ninja generator
        build_environ = get_msvc_environ(
            opts.msvs_installation_path,
            opts.msvs_version,
            opts.msvc_toolset,
            cmd_runner,
            opts.dry_run
        )
    else:
        build_environ = os.environ

    # can be empty if called for tools build
    catboost_components = get_catboost_components(opts.targets)


    cmake_cmd = [
        'cmake',
        source_root_dir,
        '-B', opts.build_root_dir,
        '-G', 'Ninja',
        f'-DCMAKE_BUILD_TYPE={opts.build_type}',
        f'-DCMAKE_TOOLCHAIN_FILE={cmake_target_toolchain}',
    ]
    if opts.verbose:
        cmake_cmd += ['--log-level=VERBOSE']
    if require_pic:
        cmake_cmd += ['-DCMAKE_POSITION_INDEPENDENT_CODE=On']
    cmake_cmd += [f'-DCATBOOST_COMPONENTS={";".join(sorted(catboost_components)) if catboost_components else "none"}']
    if platform.system().lower() == 'darwin':
        cmake_cmd += [f'-DCMAKE_OSX_DEPLOYMENT_TARGET={opts.macosx_version_min}']

    cmake_cmd += [f'-DHAVE_CUDA={"yes" if opts.have_cuda else "no"}']
    if opts.have_cuda:
        cuda_root_dir = get_cuda_root_dir(opts.cuda_root_dir)
        # CMake requires nvcc to be available in $PATH
        add_cuda_bin_path_to_system_path(build_environ, cuda_root_dir)
        cmake_cmd += [
            f'-DCUDAToolkit_ROOT={cuda_root_dir}',
            f'-DCMAKE_CUDA_RUNTIME_LIBRARY={opts.cuda_runtime_library}'
        ]

    if opts.native_built_tools_root_dir:
        cmake_cmd += [f'-DTOOLS_ROOT={opts.native_built_tools_root_dir}']

    if opts.cmake_extra_args is not None:
        cmake_cmd += opts.cmake_extra_args

    cmd_runner.run(cmake_cmd, env=build_environ)


    ninja_cmd = [
        'ninja',
        '-C', opts.build_root_dir,
    ]
    if opts.verbose:
        ninja_cmd += ['-v']
    ninja_cmd += opts.targets
    cmd_runner.run(ninja_cmd, env=build_environ)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        description=(
             disclaimer
             + ' All unrecognized arguments will be added to cmake_extra_args'
        ),
        allow_abbrev=False
    )
    for key, option in Opts.known_opts.items():
        kwargs = {
            'help': option.description,
            'required': option.required,
            'default': option.default,
            'action': 'store_true' if option.opt_type is bool else 'store'
        }
        if option.opt_type is list:
            kwargs['type'] = lambda s: s.split(',')
        elif option.opt_type is not bool:
            kwargs['type'] = option.opt_type

        parser.add_argument(
            '--' + key.replace('_', '-'),
            **kwargs
        )

    parsed_args, cmake_extra_args = parser.parse_known_args()
    parsed_args.cmake_extra_args += cmake_extra_args

    build(Opts(**vars(parsed_args)))
