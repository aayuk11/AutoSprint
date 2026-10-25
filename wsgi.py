"""WSGI entrypoint for gunicorn / uWSGI:  gunicorn wsgi:app"""

from app import app

if __name__ == "__main__":
    app.run()
