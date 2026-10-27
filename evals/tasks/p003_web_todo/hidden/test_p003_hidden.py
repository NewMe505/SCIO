"""Hidden black-box tests for p003 (M11): the released web app, driven in a real headless
browser (Playwright, Chromium) as a person would use it. The grader starts the product from
its ``## Runtime`` section and sets ``APP_URL``. The store may already hold todos from the
build's own acceptance run, so titles are unique and counts are relative."""

import json
import os
import urllib.request
import uuid

import pytest
from playwright.sync_api import expect, sync_playwright

URL = os.environ["APP_URL"]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


def test_health_reports_ok_and_the_release_version():
    with urllib.request.urlopen(URL + "/health", timeout=10) as res:
        assert json.load(res) == {"status": "ok", "version": "0.1.0"}


def test_the_page_has_its_heading_form_and_list(page):
    page.goto(URL + "/")
    expect(page.get_by_role("heading", name="Todos")).to_be_visible()
    expect(page.locator('input[name="title"]')).to_be_visible()
    expect(page.get_by_role("button", name="Add")).to_be_visible()
    expect(page.locator("#todos")).to_be_attached()


def test_a_todo_added_in_the_form_is_listed_last_and_stays_after_a_reload(page):
    title = f"buy milk {uuid.uuid4().hex[:6]}"
    page.goto(URL + "/")
    page.fill('input[name="title"]', title)
    page.get_by_role("button", name="Add").click()
    expect(page.locator("#todos li").last).to_have_text(title)
    page.reload()
    expect(page.locator("#todos li", has_text=title)).to_have_count(1)


def test_an_empty_title_shows_the_error_and_adds_nothing(page):
    page.goto(URL + "/")
    before = page.locator("#todos li").count()
    page.fill('input[name="title"]', "   ")
    page.get_by_role("button", name="Add").click()
    expect(page.locator(".error")).to_contain_text("title is required")
    page.goto(URL + "/")
    expect(page.locator("#todos li")).to_have_count(before)
