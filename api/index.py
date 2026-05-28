import sys
import os

# Add parent directory to path so app.py can be imported by Vercel
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
