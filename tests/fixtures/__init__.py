"""Fixtures shared across test modules.

A fixture two suites need lives here rather than being imported sideways out of whichever
suite happened to write it first. `test_reconcile` and `test_graph` both verify the committed
demo workbook, and a second hand-authored mapping would let them drift into testing different
workbooks while both looking correct.
"""
