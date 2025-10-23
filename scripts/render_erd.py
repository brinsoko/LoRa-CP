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

    graph.write_png("erd.png")
    graph.write_pdf("erd.pdf")

print("ERD generated: erd.png and erd.pdf")