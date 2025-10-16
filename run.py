# run.py — single entrypoint
from app import create_app

app = create_app()

if __name__ == "__main__":
    # Use debug=True locally only
    app.run(debug=True)