"""
Arkim Procurement Agent — Streamlit entry point.

Run with:  streamlit run app.py
"""
from dotenv import load_dotenv
load_dotenv()

import streamlit as st

st.switch_page("pages/sourcing_runs.py")
