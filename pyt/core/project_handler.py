"""Generates a list of CFGs from a path.

The module finds all python modules and generates an ast for them.
"""
import logging
import os


log = logging.getLogger(__name__)


# Isolated per-directory cache: modules of one directory never leak
# into the module list of another directory or project.
_directory_modules_cache = dict()


def get_directory_modules(directory):
    """Return a list containing tuples of
    e.g. ('__init__', 'example/import_test_project/__init__.py')

    Results are cached per directory, so analysing multiple projects
    in one run does not mix their modules.
    """
    if not os.path.isdir(directory):
        # example/import_test_project/A.py -> example/import_test_project
        directory = os.path.dirname(directory)

    directory = os.path.normpath(directory)

    if directory in _directory_modules_cache:
        return _directory_modules_cache[directory]

    modules = list()
    if os.path.isdir(directory):
        for path in os.listdir(directory):
            if _is_python_file(path):
                # A.py -> A
                module_name = os.path.splitext(path)[0]
                modules.append((module_name, os.path.join(directory, path)))

    _directory_modules_cache[directory] = modules
    return modules


def get_modules(path, prepend_module_root=True):
    """Return a list containing tuples of
    e.g. ('test_project.utils', 'example/test_project/utils.py')

    Module names are derived relative to the given project root instead
    of guessing with the basename, so equally named directories elsewhere
    in the path cannot corrupt the names. Ambiguous module names (same
    name mapping to different files) are reported as a risk.
    """
    project_path = path
    module_root = os.path.split(os.path.normpath(project_path))[1]
    modules = list()
    seen = dict()
    for root, directories, filenames in os.walk(project_path):
        for filename in filenames:
            if _is_python_file(filename):
                relative_directory = os.path.relpath(root, project_path)
                if relative_directory == os.curdir:
                    directory = ''
                else:
                    directory = relative_directory.replace(os.sep, '.')

                module_name_parts = []
                if prepend_module_root:
                    module_name_parts.append(module_root)
                if directory:
                    module_name_parts.append(directory)

                if filename == '__init__.py':
                    path = root
                else:
                    module_name_parts.append(os.path.splitext(filename)[0])
                    path = os.path.join(root, filename)

                module_name = '.'.join(module_name_parts)
                if module_name in seen and seen[module_name] != path:
                    log.warning(
                        'Ambiguous module name %s: both %s and %s match; '
                        'imports of it may resolve to the wrong implementation.',
                        module_name,
                        seen[module_name],
                        path
                    )
                else:
                    seen[module_name] = path
                modules.append((module_name, path))

    return modules


class ModuleIndex:
    """Isolated, reusable module index for a single analysis run.

    Built once per analysed project from an explicit project root.
    Absolute and relative imports are resolved against that root;
    imports that escape the root or are ambiguous are reported as
    risks instead of silently borrowing modules from elsewhere.
    """

    def __init__(self, project_root, prepend_module_root=True):
        self.project_root = os.path.realpath(project_root)
        self._prepend_module_root = prepend_module_root
        self.project_modules = get_modules(
            project_root,
            prepend_module_root=prepend_module_root
        )
        self.risks = list()
        self._module_map = dict()
        for module_name, module_path in self.project_modules:
            existing = self._module_map.get(module_name)
            if existing is not None and existing != module_path:
                self._report_risk(
                    'Ambiguous module {}: both {} and {} match.'.format(
                        module_name, existing, module_path
                    )
                )
            else:
                self._module_map[module_name] = module_path

    def _report_risk(self, message):
        self.risks.append(message)
        log.warning('%s', message)

    def local_modules(self, path):
        """Return the cached local modules of the directory of path."""
        return get_directory_modules(path)

    def _is_within_project(self, path):
        return os.path.commonpath(
            (self.project_root, os.path.realpath(path))
        ) == self.project_root

    def _module_name_for_path(self, path):
        relative = os.path.relpath(
            os.path.realpath(path),
            os.path.dirname(self.project_root)
        )
        name = relative.replace(os.sep, '.')
        if name.endswith('.py'):
            name = name[:-3]
        if not self._prepend_module_root:
            root_name = os.path.split(self.project_root)[1]
            prefix = root_name + '.'
            if name.startswith(prefix):
                name = name[len(prefix):]
        return name

    def resolve_absolute_import(self, module_name, from_file=None):
        """Resolve `import module_name` against the project root.

        Returns a (module_name, path) tuple or None when the module is
        not part of the project (e.g. a blackbox or built-in module).
        """
        if from_file:
            for local_name, local_path in self.local_modules(from_file):
                if local_name == module_name:
                    project_path = self._module_map.get(module_name)
                    if project_path and os.path.realpath(project_path) != os.path.realpath(local_path):
                        self._report_risk(
                            'Ambiguous import {}: matches both local {} and '
                            'project module {}.'.format(
                                module_name, local_path, project_path
                            )
                        )
                    return (local_name, local_path)
        project_path = self._module_map.get(module_name)
        if project_path is not None:
            return (module_name, project_path)
        return None

    def resolve_relative_import(self, module, level, from_file):
        """Resolve `from .module import ...` relative to from_file.

        Returns a (module_name, path) tuple, or None when the import
        escapes the explicit project root (reported as a risk).
        """
        if level < 1:
            raise ValueError('Relative imports need a level of at least 1.')
        base = os.path.dirname(os.path.realpath(from_file))
        for _ in range(level - 1):
            base = os.path.dirname(base)
        if module:
            target = os.path.normpath(
                os.path.join(base, module.replace('.', os.sep))
            )
        else:
            target = base

        if not self._is_within_project(target):
            self._report_risk(
                'Relative import {!r} from {} escapes the project root {}; '
                'not resolving it.'.format(
                    '.' * level + (module or ''), from_file, self.project_root
                )
            )
            return None

        if os.path.isdir(target):
            return (self._module_name_for_path(target), target)
        if os.path.isfile(target + '.py'):
            return (self._module_name_for_path(target), target + '.py')
        return None


def _is_python_file(path):
    if os.path.splitext(path)[1] == '.py':
        return True
    return False
