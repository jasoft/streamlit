import json
from pathlib import Path
from typing import Any, Dict
import streamlit as st

STATE_FILE = Path(__file__).resolve().parent / ".slider_state.json"

def load_slider_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_slider_state(key: str, value: Any) -> None:
    state = load_slider_state()
    state[key] = value
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

def on_slider_change(key: str):
    if key in st.session_state:
        save_slider_state(key, st.session_state[key])

def init_slider_state(key: str, default_value: Any, min_value: Any = None, max_value: Any = None) -> Any:
    """Initialize st.session_state[key] with the saved value, clipped to [min_value, max_value]."""
    state = load_slider_state()
    val = state.get(key, default_value)
    
    # Clip the value if bounds are provided
    if min_value is not None:
        val = max(val, min_value)
    if max_value is not None:
        val = min(val, max_value)
        
    st.session_state[key] = val
    return val
