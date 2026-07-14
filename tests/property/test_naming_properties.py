from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st

from ai_organizer.domain.naming import INVALID_WINDOWS, builtin_naming_profiles


@given(
    st.text(min_size=1, max_size=400).filter(
        lambda value: bool(re.sub(r"\s+", " ", INVALID_WINDOWS.sub("", value)).strip(" .-_\t"))
    )
)
def test_rendered_filename_is_bounded_and_windows_safe(title: str) -> None:
    profile = builtin_naming_profiles()[-1]
    rendered = profile.render({"clean_title": title}, ".pdf")
    assert len(rendered) <= profile.max_component_length
    assert not INVALID_WINDOWS.search(rendered)
    assert not rendered.endswith((" ", "."))
