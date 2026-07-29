# Fuzz corpus

Seed inputs for `tests/test_fuzz_loader.py`. Every file here is something the
loader must **handle cleanly**: come back at all, refuse in writing rather than
by traceback, quote back a bounded amount of what it refused, and spend bounded
time and memory doing it. Most of them must also actually be refused;
`MUST_FAIL` in the test lists which, so that a seed which quietly starts loading
is noticed rather than counted as a pass.

They are seeds, not the corpus. The test mutates them — truncating, splicing,
repeating, re-indenting and nesting — and Hypothesis keeps whatever it finds
interesting in `.hypothesis/examples`.

Each one is as small as it can be and still cross the threshold it is about: the
digit-limit seeds carry 4400 digits because CPython refuses to convert 4300, and
the nesting seed is 1200 levels because the loader's limit is 1024. The
genuinely enormous inputs — a megabyte-long scalar, fifty thousand levels of
nesting — are generated in the test rather than committed, under `AMPLIFIERS`.

Deliberately *not* under `tests/fixtures/`: everything there is collected by
`tests/test_fmt.py`, which asserts the formatter's properties on every document
the repository ships. These are not documents, they are attacks, and several of
them are not YAML at all.
