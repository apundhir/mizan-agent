# VIOLATION FIXTURE — a credential in a tracked file

The secret guard scans what **git tracks**, so this fixture is a real repository: a directory tree
alone would let the guard find nothing and report success, which is the failure mode the guard's own
`tracked_files` raises about.

`tests/arch/test_guards.py` initialises it and stages the files. It is not a nested repository in
this one — `.git` is created in a temporary copy at test time, so nothing here is committed as a
submodule.
