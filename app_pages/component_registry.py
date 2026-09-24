"""Register each fixed inline component once per Streamlit runtime registry.

A module can outlive an AppTest Runtime or a development runtime reload. A
module-global renderer alone then points at a definition that no longer exists.
This identity-scoped cache uses Streamlit 1.63's own manager selector while
registration and mounting still use the public v2 API.
"""
from threading import RLock
from weakref import WeakKeyDictionary

_RENDERERS=WeakKeyDictionary()
_LOCK=RLock()


def inline_component(name, *, html, css, js):
    import streamlit as st
    from streamlit.components.v2.get_bidi_component_manager import get_bidi_component_manager
    manager=get_bidi_component_manager()
    signature=(html,css,js)
    with _LOCK:
        cache=_RENDERERS.setdefault(manager,{})
        stored=cache.get(name)
        if stored is None or stored[0]!=signature:
            renderer=st.components.v2.component(name,html=html,css=css,js=js)
            cache[name]=(signature,renderer)
        return cache[name][1]
