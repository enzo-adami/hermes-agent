import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { describe, expect, it } from "vitest";

type ElementNode = {
  children?: unknown[];
};

type RegisteredKanbanPage = {
  TaskFilesSection: (props: Record<string, unknown>) => unknown;
};

describe("Kanban task files plugin", () => {
  it("renders input attachments and generated artifacts as distinct groups", () => {
    const registration: { page?: RegisteredKanbanPage } = {};
    const createElement = (
      _type: unknown,
      _props: Record<string, unknown> | null,
      ...children: unknown[]
    ): ElementNode => ({ children: children.flat(Number.POSITIVE_INFINITY) });

    const sandbox = {
      console,
      window: {
        confirm: () => true,
        localStorage: { getItem: () => null, setItem: () => undefined },
        __HERMES_PLUGINS__: {
          register: (_name: string, component: RegisteredKanbanPage) => {
            registration.page = component;
          },
        },
        __HERMES_PLUGIN_SDK__: {
          React: {
            createElement,
            Fragment: "fragment",
            Component: class {
              props: unknown;
              state = {};
              constructor(props: unknown) {
                this.props = props;
              }
            },
          },
          components: {
            Card: "Card",
            CardContent: "CardContent",
            Badge: "Badge",
            Button: "Button",
            Input: "Input",
            Label: "Label",
            Select: "Select",
            SelectOption: "SelectOption",
          },
          hooks: {
            useState: (initial: unknown) => [
              typeof initial === "function" ? (initial as () => unknown)() : initial,
              () => undefined,
            ],
            useEffect: () => undefined,
            useCallback: (fn: unknown) => fn,
            useMemo: (fn: () => unknown) => fn(),
            useRef: (value: unknown) => ({ current: value }),
          },
          utils: {
            cn: (...parts: unknown[]) => parts.filter(Boolean).join(" "),
            timeAgo: () => "",
          },
          fetchJSON: () => Promise.resolve({}),
          authedFetch: () => Promise.resolve({ ok: true }),
        },
      },
    };

    const bundle = path.resolve(
      import.meta.dirname,
      "../../../plugins/kanban/dashboard/dist/index.js",
    );
    vm.runInNewContext(fs.readFileSync(bundle, "utf8"), sandbox, { filename: bundle });

    expect(registration.page).toBeDefined();
    const page = registration.page;
    if (!page) throw new Error("Kanban plugin did not register its page");
    expect(typeof page.TaskFilesSection).toBe("function");
    const tree = page.TaskFilesSection({
      i18n: null,
      attachments: [
        { id: 1, filename: "brief.txt", size: 5, attachment_type: "attachment" },
        { id: 2, filename: "report.pdf", size: 9, attachment_type: "artifact" },
        { id: 3, filename: "legacy.txt", size: 1, attachment_type: null },
      ],
      onUpload: () => undefined,
      onDelete: () => undefined,
      uploadBusy: false,
    });

    const texts: string[] = [];
    const walk = (node: unknown): void => {
      if (node == null || typeof node === "boolean") return;
      if (typeof node === "string" || typeof node === "number") {
        texts.push(String(node));
        return;
      }
      if (Array.isArray(node)) {
        node.forEach(walk);
        return;
      }
      walk((node as ElementNode).children);
    };
    walk(tree);

    const text = texts.join(" ");
    expect(text).toContain("Attachments (2)");
    expect(text).toContain("Artifacts (1)");
    expect(text.indexOf("brief.txt")).toBeLessThan(text.indexOf("Artifacts (1)"));
    expect(text.indexOf("Artifacts (1)")).toBeLessThan(text.indexOf("report.pdf"));
  });
});