"""The nine launcher icons, and the three places their names have to agree.

An icon that exists but is not declared is dead weight; a name declared but not generated is a
blank square on someone's home screen, and on Android a manifest referring to a missing drawable
fails the build. None of that is visible from any one file, so it is asserted across all of them.

The generator itself lives in the mobile repo (`tool/city_icons.py`) because that is where the
assets go, but the colours come from these city YAMLs, which is why the check belongs here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

API = Path(__file__).resolve().parents[1]
MOBILE = API.parent / "opentransit-mobile"
pytestmark = pytest.mark.skipif(not MOBILE.exists(), reason="mobile checkout not alongside")


def city_colours() -> dict[str, str]:
    out = {}
    for f in sorted((API / "cities").glob("*.yaml")):
        if f.stem.startswith("_"):
            continue
        d = yaml.safe_load(f.read_text())
        out[f.stem] = (d.get("branding") or {}).get("primary_color", "").upper()
    return out


def test_every_city_has_a_colour_the_generator_can_use():
    for city, colour in city_colours().items():
        assert re.fullmatch(r"#[0-9A-F]{6}", colour), f"{city}: {colour!r}"


def test_the_android_manifest_declares_an_alias_for_every_city_and_no_others():
    manifest = (MOBILE / "android/app/src/main/AndroidManifest.xml").read_text()
    declared = set(re.findall(r'android:name="\.Launcher(\w+)"', manifest))
    declared.discard("")
    expected = {c.capitalize() for c in city_colours()}
    assert declared == expected

    # Each alias points at the real activity and ships disabled; exactly one default is enabled.
    aliases = re.findall(r"<activity-alias(.*?)</activity-alias>", manifest, re.S)
    assert len(aliases) == len(expected) + 1
    enabled = [a for a in aliases if 'android:enabled="true"' in a]
    assert len(enabled) == 1, "exactly one launcher icon may ship enabled"
    assert all('android:targetActivity=".MainActivity"' in a for a in aliases)


def test_main_activity_keeps_its_deep_links_and_never_the_launcher():
    """The whole reason the launcher entry moved onto aliases.

    MainActivity carries the App Links, the geo: handler and the share target, so it can never be
    the component that gets disabled to hide an icon."""
    manifest = (MOBILE / "android/app/src/main/AndroidManifest.xml").read_text()
    activity = manifest[manifest.index("<activity"):manifest.index("</activity>")]
    assert 'android:scheme="geo"' in activity
    assert "android.intent.action.SEND" in activity
    assert "opentransit.tech" in activity
    assert "android.intent.category.LAUNCHER" not in activity


def test_ios_declares_exactly_the_icon_sets_that_exist():
    xcconfig = (MOBILE / "ios/Flutter/Release.xcconfig").read_text()
    m = re.search(r"ASSETCATALOG_COMPILER_ALTERNATE_APPICON_NAMES = (.+)", xcconfig)
    assert m, "without this setting the alternate icons compile to nothing"
    declared = set(m.group(1).split())
    assert declared == {f"AppIcon-{c}" for c in city_colours()}

    for name in declared:
        d = MOBILE / "ios/Runner/Assets.xcassets" / f"{name}.appiconset"
        assert d.is_dir(), f"{name} is declared but has no icon set"
        contents = json.loads((d / "Contents.json").read_text())
        files = {i["filename"] for i in contents["images"]}
        assert files, f"{name} declares no images"
        for f in files:
            assert (d / f).exists(), f"{name}: {f} is listed but missing"


def test_the_android_colour_matches_the_city_the_app_draws_with():
    """An icon in a colour the app does not use would look like a different product."""
    colours = (MOBILE / "android/app/src/main/res/values/colors.xml").read_text()
    for city, want in city_colours().items():
        m = re.search(rf'name="ic_launcher_background_{city}">(#[0-9A-Fa-f]{{6}})<', colours)
        assert m, f"no launcher background for {city}"
        assert m.group(1).upper() == want


def test_the_channel_knows_the_same_nine_cities():
    """Kotlin holds its own list, because a method channel cannot ask Dart what is valid."""
    kt = (MOBILE / "android/app/src/main/kotlin/org/opentransit/opentransit_mobile"
          / "CityIconBridge.kt").read_text()
    listed = set(re.findall(r'"([a-z]+)",', kt[kt.index("private val CITIES"):]))
    assert listed == set(city_colours())
