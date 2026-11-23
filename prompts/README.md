# prompts/

Versioned prompt files (`brand.md`, `router.md`, `judge.md`, ...). Code loads prompts by name;
the version recorded in traces is the filename plus a hash of the contents. Long prompt strings
never live inside Python.
