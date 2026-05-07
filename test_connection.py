import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

engine = create_engine(os.getenv("DATABASE_URL"))

with engine.connect() as conn:
    print("db:", conn.execute(text("SELECT current_database()")).scalar())
    print("port:", conn.execute(text("SHOW port")).scalar())
    print("version:", conn.execute(text("SELECT version()")).scalar())
    print("databases:", conn.execute(text("""
        SELECT datname FROM pg_database
        ORDER BY datname
    """)).all())