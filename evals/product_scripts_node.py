"""Scripted feature work for the Fake ``todo_api`` product in TypeScript (M10, ADR 0014): the
same two features as ``product_scripts_http.py`` (health endpoint, create and list todos)
on the ``node-ts`` profile, so one product spec builds in both stacks through the whole
milestone loop offline.

The store is a JSON file under ``.scio/sandbox/`` (git-ignored) written with ``node:fs``:
no third-party dependency, because a Node product has no host environment to borrow from
(``npm install`` would need the registry) and the Fake must run with the network off. The
tests listen the app (``createApp``) on a free port in the test process and talk to it with
``fetch``: the coverage floor counts only in-process code (M11, I-091); the black-box
acceptance tests run against the started entry file.
"""

from __future__ import annotations

from evals.fake_scripts import Edit, Step, TaskScript

PKG = "todo_api"


def scaffold_node_http_cli(pkg: str) -> str:
    """The ``src/cli.ts`` the node-ts scaffold ships for an http product (the first edit's
    old text)."""
    from scio.product.scaffold import node_ts_scaffold
    from scio.state.models import AppSpec, ProductFeature, ProductSpec

    spec = ProductSpec(
        name=pkg,
        title=pkg,
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="d", acceptance=["a"])],
        quality_bar=["q"],
        stack="node-ts",
        app=AppSpec(kind="http", args=["serve", "--port", "{port}"]),
    )
    return node_ts_scaffold(spec)["src/cli.ts"]


SERVER_HELPERS = """import type { Server } from "node:http";
import type { AddressInfo } from "node:net";

/** Listens on a free port in this process (the coverage floor counts in-process code). */
export async function listen(server: Server): Promise<string> {
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const { port } = server.address() as AddressInfo;
  return `http://127.0.0.1:${port}`;
}
"""

HEALTH_UNIT_TESTS = """import { test } from "node:test";
import assert from "node:assert/strict";

import { createApp } from "../src/cli.ts";
import { VERSION } from "../src/version.ts";
import { listen } from "./helpers.ts";

test("GET /health reports ok and the version", async () => {
  const server = createApp();
  try {
    const res = await fetch(`${await listen(server)}/health`);
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { status: "ok", version: VERSION });
  } finally {
    server.close();
  }
});
"""

STORE = """/** JSON-file store for todos (node:fs, no third-party dependency). */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export interface Todo {
  id: number;
  title: string;
  done: boolean;
}

export class Store {
  // erasable syntax only: no parameter properties, Node strips types and nothing more
  private readonly path: string;

  constructor(path: string) {
    this.path = path;
  }

  load(): Todo[] {
    if (!existsSync(this.path)) {
      return [];
    }
    return JSON.parse(readFileSync(this.path, "utf8")) as Todo[];
  }

  save(items: Todo[]): void {
    mkdirSync(dirname(this.path), { recursive: true });
    writeFileSync(this.path, JSON.stringify(items, null, 2) + "\\n", "utf8");
  }

  add(title: string): Todo {
    const items = this.load();
    const todo: Todo = { id: items.length + 1, title, done: false };
    items.push(todo);
    this.save(items);
    return todo;
  }
}

export function defaultStore(): Store {
  return new Store(process.env.TODO_FILE ?? ".scio/sandbox/todos.json");
}
"""

CLI_P2 = """/** Command line entry point: health plus the todos resource. */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

import { defaultStore, type Store } from "./store.ts";
import { VERSION } from "./version.ts";

export const HEALTH_PATH = "/health";

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
  });
}

/** The app, not yet listening: the tests listen on port 0 in-process. */
export function createApp(store: Store = defaultStore()): Server {
  return createServer(async (req, res) => {
    if (req.method === "GET" && req.url === HEALTH_PATH) {
      sendJson(res, 200, { status: "ok", version: VERSION });
      return;
    }
    if (req.method === "GET" && req.url === "/todos") {
      sendJson(res, 200, store.load());
      return;
    }
    if (req.method === "POST" && req.url === "/todos") {
      let title = "";
      try {
        const parsed = JSON.parse(await readBody(req)) as { title?: unknown };
        title = typeof parsed.title === "string" ? parsed.title.trim() : "";
      } catch {
        title = "";
      }
      if (!title) {
        sendJson(res, 400, { error: "title is required" });
        return;
      }
      sendJson(res, 201, store.add(title));
      return;
    }
    res.writeHead(404);
    res.end();
  });
}

/* node:coverage disable */
export function serve(port: number): number {
  const server = createApp();
  const host = process.env.HOST ?? "127.0.0.1";
  server.listen(port, host, () => {
    console.log(`serving on http://${host}:${port}`);
  });
  process.on("SIGTERM", () => server.close());
  return 0;
}
/* node:coverage enable */

export function main(argv: readonly string[]): number {
  if (argv[0] === "--version") {
    console.log(VERSION);
    return 0;
  }
  /* node:coverage disable */
  if (argv[0] === "serve") {
    const at = argv.indexOf("--port");
    const port = at >= 0 ? Number(argv[at + 1]) : 8000;
    return serve(port);
  }
  /* node:coverage enable */
  console.error("usage: node src/main.ts --version | serve --port N");
  return 2;
}
"""

TODOS_UNIT_TESTS = """import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createApp } from "../src/cli.ts";
import { Store } from "../src/store.ts";
import { listen } from "./helpers.ts";

const tempFile = (): string => join(mkdtempSync(join(tmpdir(), "todos-")), "todos.json");

test("POST /todos then GET /todos returns the todo", async () => {
  const file = tempFile();
  const server = createApp(new Store(file));
  try {
    const base = await listen(server);
    const created = await fetch(`${base}/todos`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "Buy milk" }),
    });
    assert.equal(created.status, 201);
    const todo = (await created.json()) as { id: number; title: string; done: boolean };
    assert.equal(todo.id, 1);
    assert.equal(todo.done, false);
    const listed = await fetch(`${base}/todos`);
    assert.deepEqual(await listed.json(), [todo]);
    assert.ok(existsSync(file));
  } finally {
    server.close();
  }
});

test("a todo needs a title, and unknown paths are 404", async () => {
  const server = createApp(new Store(tempFile()));
  try {
    const base = await listen(server);
    const post = (body: string) => fetch(`${base}/todos`, { method: "POST", body });
    assert.equal((await post("not json")).status, 400);
    assert.equal((await post("{}")).status, 400);
    assert.equal((await fetch(`${base}/missing`)).status, 404);
  } finally {
    server.close();
  }
});
"""

TODOS_ACCEPTANCE = """import { test } from "node:test";
import assert from "node:assert/strict";

const APP_URL = process.env.APP_URL;

test(
  "create and list todos over HTTP",
  { skip: APP_URL ? false : "APP_URL not set: scio build starts the app" },
  async () => {
    const created = await fetch(`${APP_URL}/todos`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "Write report" }),
    });
    assert.equal(created.status, 201);
    const todo = (await created.json()) as { id: number; title: string };
    const listed = await fetch(`${APP_URL}/todos`);
    const items = (await listed.json()) as { id: number; title: string }[];
    assert.ok(items.some((item) => item.id === todo.id && item.title === "Write report"));
  },
);
"""


def _changelog(line: str) -> Edit:
    return Edit("CHANGELOG.md", "### Added\n", f"### Added\n- {line}\n")


NODE_PRODUCT_SCRIPTS: list[TaskScript] = [
    TaskScript(
        key="health and version endpoint",
        tier="T0",
        features=[("F1", "GET /health returns status ok and the version", "health")],
        steps=[
            Step(
                "S1",
                "prove the health endpoint with an in-process test of the app",
                ["F1"],
                ["tests/helpers.ts", "tests/health.test.ts"],
                edits=[
                    Edit("tests/helpers.ts", "", SERVER_HELPERS),
                    Edit("tests/health.test.ts", "", HEALTH_UNIT_TESTS),
                ],
                test_filter="health",
            )
        ],
        docs=[_changelog("Health endpoint test (P1).")],
    ),
    TaskScript(
        key="create and list todos",
        tier="T1",
        features=[("F1", "POST /todos then GET /todos returns the todo as JSON", "todos")],
        steps=[
            Step(
                "S1",
                "todos resource over HTTP with a JSON-file store",
                ["F1"],
                ["src/store.ts", "src/cli.ts", "tests/todos.test.ts"],
                edits=[
                    Edit("src/store.ts", "", STORE),
                    Edit("src/cli.ts", scaffold_node_http_cli(PKG), CLI_P2),
                    Edit("tests/todos.test.ts", "", TODOS_UNIT_TESTS),
                    Edit("tests/acceptance/todos.test.ts", "", TODOS_ACCEPTANCE),
                ],
                test_filter="todos",
            )
        ],
        docs=[_changelog("`POST /todos` and `GET /todos` with a JSON-file store (P2).")],
    ),
]
