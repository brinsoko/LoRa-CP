import sys, os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy_schemadisplay import create_schema_graph
from app import create_app
from app.extensions import db

app = create_app()

with app.app_context():
    engine = db.get_engine()
    md = db.Model.metadata

    graph = create_schema_graph(
        engine=engine,                
        metadata=md,
        show_datatypes=True,          # show column types
        show_indexes=False,           # omit indexes for clarity
        rankdir="LR",                 # layout left → right
        concentrate=False,            # avoid overlapping lines
    )

    graph.write_png("docs/erd.png") # type: ignore
    graph.write_pdf("docs/erd.pdf") # type: ignore

print("ERD generated: docs/erd.png and docs/erd.pdf")