"""Smoke test: the real app starts (uvicorn, throwaway DB) and the main pages
and API routes -- old and new -- answer 200."""

import httpx
import pytest

from tests.live_server import AUTH, live_server

ROUTES = [
    "/",
    "/healthz",
    "/static/app.js",
    "/static/style.css",
    "/static/tracks.js",
    "/static/manifest.json",
    "/api/sessions",
    "/api/daily-metrics",
    "/api/journal",
    "/api/dashboard/summary",
    "/api/coach/conversations",
    "/api/coach/intents",
    "/api/calendar",
    "/api/tracks",
    "/api/tracks/options",
    "/api/tracks/trends",
    "/api/gear",
    "/api/gear/setups",
    "/api/sailing/config",
    "/api/sailing/baselines",
    "/api/sailing/focus",
    "/api/sync",
    # gym tracker
    "/gym/",
    "/gym/sw.js",
    "/gym/manifest.webmanifest",
    "/static/gym/gym.js",
    "/static/gym/gym.css",
    "/static/gym-analysis.js",
    "/api/gym/bootstrap",
    "/api/gym/sessions",
    "/api/gym/locations",
    "/api/gym/exercises",
    "/api/gym/volume",
    # plan tab
    "/static/plans.js",
    "/api/plans",
    # weight & nutrition
    "/food/",
    "/food/sw.js",
    "/food/manifest.webmanifest",
    "/static/food/food.js",
    "/static/food/food.css",
    "/static/weight-chart.js",
    "/static/nutrition.js",
    "/api/nutrition/bootstrap",
    "/api/nutrition/summary",
    "/api/nutrition/targets",
    "/api/nutrition/weights",
    "/api/nutrition/weight",
    "/api/nutrition/foods",
    "/api/nutrition/saved-meals",
    "/api/nutrition/day/2026-11-01",
]


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    with live_server(tmp_path_factory.mktemp("smoke") / "smoke.db") as url:
        yield url


@pytest.mark.parametrize("path", ROUTES)
def test_route_responds(base, path):
    r = httpx.get(base + path, auth=AUTH, timeout=20)
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"


def test_auth_still_required(base):
    assert httpx.get(base + "/", timeout=10).status_code == 401
    assert httpx.get(base + "/api/gym/bootstrap", timeout=10).status_code == 401
    assert httpx.get(base + "/gym-login", timeout=10).status_code == 401
    assert httpx.get(base + "/food/", timeout=10).status_code == 401
    assert httpx.get(base + "/api/nutrition/bootstrap", timeout=10).status_code == 401
    r = httpx.get(base + "/gym-login", auth=AUTH, timeout=10, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (307, "/gym/")


def test_index_has_gym_tab(base):
    html = httpx.get(base + "/", auth=AUTH, timeout=10).text
    assert 'data-tab="gym"' in html and "/static/gym-analysis.js" in html
    assert 'data-tab="plan"' in html and "/static/plans.js" in html
    # Plan sits between Journal and Coach
    assert html.index('data-tab="journal"') < html.index('data-tab="plan"') < html.index('data-tab="coach"')


def test_index_has_nutrition_tab_and_dashboard_tiles(base):
    html = httpx.get(base + "/", auth=AUTH, timeout=10).text
    assert 'data-tab="nutrition"' in html and "/static/nutrition.js" in html and "/static/weight-chart.js" in html
    assert html.index('data-tab="gym"') < html.index('data-tab="nutrition"') < html.index('data-tab="journal"')
    assert 'id="statWeight"' in html and 'id="statProtein"' in html
    summary = httpx.get(base + "/api/dashboard/summary", auth=AUTH, timeout=10).json()
    assert summary["nutrition"]["protein_target_g"] == 160
