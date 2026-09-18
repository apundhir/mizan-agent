"""VIOLATION FIXTURE - the tautology from the other direction.

The generator knows the exact x position it drew every column at. The extractor needs those
positions. Importing them is one line, removes a "duplicated" table, and looks like a cleanup.

It would make the committed column map a round-trip through the renderer's own constants: the
extractor would verify the renderer against itself and go on passing after the layout stopped
matching anything a real property would send. The map has to be measured from the rendered
document, which is what a parser written against a real export would have to do.

Rule 2 forbids the generator importing the product. This is the mirror, and the narrower rule
would have accepted it.
"""

from datagen.render_pdf import COLUMNS


def cell_ranges() -> list[tuple[float, float]]:
    return [(column.x, column.x + column.width) for column in COLUMNS]
