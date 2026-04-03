# database.py

from sqlalchemy import create_engine, Column, Integer, String, Text
# If you need to parse URLs, use:
# from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# This creates a file called 'notebook.db' in your folder
SQLALCHEMY_DATABASE_URL = "sqlite:///./notebook.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class LabNote(Base):
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    content = Column(Text)
    pubmed_ref = Column(String)

Base.metadata.create_all(bind=engine)
