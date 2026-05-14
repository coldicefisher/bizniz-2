#!/usr/bin/env node
/**
 * TypeScript documenter sidecar.
 *
 * Walks a workspace mounted at /workspace and emits a structured
 * JSON contract describing every source file's exports, imports,
 * interfaces, type aliases, and zustand-style store shapes.
 *
 * Output goes to stdout. The Python orchestrator
 * (bizniz/documenters/typescript_ast.py) parses it.
 *
 * Output shape mirrors PythonAstDocumenter for uniformity.
 */
const path = require("path");
const fs = require("fs");
const { Project, SyntaxKind } = require("ts-morph");

const WORKSPACE = process.argv[2] || "/workspace";
const SERVICE = process.argv[3] || "";

// Mirror the Python documenter's skip rules.
const SKIP_DIRS = new Set([
  "node_modules", "dist", "build", ".next", ".nuxt", ".svelte-kit",
  ".turbo", ".parcel-cache", ".vite", "coverage", "out",
  ".git", ".idea", ".vscode",
  "tests", "__tests__", "test",
]);
const SKIP_SUFFIXES = [
  ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
  ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
  ".d.ts",
];
const INCLUDE_EXTS = [".ts", ".tsx", ".js", ".jsx"];

function shouldSkip(absPath) {
  const rel = path.relative(WORKSPACE, absPath);
  const parts = rel.split(path.sep);
  if (parts.slice(0, -1).some((p) => SKIP_DIRS.has(p))) return true;
  const name = path.basename(absPath);
  if (SKIP_SUFFIXES.some((s) => name.endsWith(s))) return true;
  return false;
}

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      yield* walk(full);
    } else if (entry.isFile()) {
      const ext = path.extname(entry.name);
      if (!INCLUDE_EXTS.includes(ext)) continue;
      if (shouldSkip(full)) continue;
      yield full;
    }
  }
}

// Initialize ts-morph with permissive compiler options so we don't
// fail on missing references — we want to extract whatever's there
// even if the project doesn't fully type-check.
const project = new Project({
  compilerOptions: {
    target: 99, // ESNext
    module: 99, // ESNext
    jsx: 4,     // ReactJSX
    allowJs: true,
    checkJs: false,
    noEmit: true,
    skipLibCheck: true,
    isolatedModules: true,
  },
  skipAddingFilesFromTsConfig: true,
  skipFileDependencyResolution: true,
  useInMemoryFileSystem: false,
});

const filePaths = [];
for (const f of walk(WORKSPACE)) filePaths.push(f);
filePaths.forEach((f) => project.addSourceFileAtPath(f));

const out = {
  service: SERVICE,
  language: "typescript",
  framework_hints: new Set(),
  files: {},
};

// ── extractors ──────────────────────────────────────────────────────

function paramText(p) {
  return {
    name: p.getName(),
    type: p.getTypeNode() ? p.getTypeNode().getText() : null,
    default: p.getInitializer() ? p.getInitializer().getText() : null,
  };
}

function extractFunction(decl) {
  return {
    kind: "function",
    name: decl.getName ? decl.getName() : null,
    async: decl.isAsync ? decl.isAsync() : false,
    default: decl.isDefaultExport ? decl.isDefaultExport() : false,
    params: (decl.getParameters ? decl.getParameters() : []).map(paramText),
    return_type: decl.getReturnTypeNode ? (decl.getReturnTypeNode() ? decl.getReturnTypeNode().getText() : null) : null,
  };
}

function extractClass(decl) {
  return {
    kind: "class",
    name: decl.getName ? decl.getName() : null,
    default: decl.isDefaultExport ? decl.isDefaultExport() : false,
    extends: decl.getExtends ? (decl.getExtends() ? decl.getExtends().getText() : null) : null,
    methods: (decl.getMethods ? decl.getMethods() : []).map((m) => ({
      name: m.getName(),
      params: m.getParameters().map(paramText),
      return_type: m.getReturnTypeNode() ? m.getReturnTypeNode().getText() : null,
    })),
    properties: (decl.getProperties ? decl.getProperties() : []).map((p) => ({
      name: p.getName(),
      type: p.getTypeNode() ? p.getTypeNode().getText() : null,
    })),
  };
}

function extractInterface(decl) {
  return {
    name: decl.getName(),
    extends: decl.getExtends().map((e) => e.getText()),
    members: decl.getMembers().map((m) => {
      const kind = m.getKindName();
      if (kind === "PropertySignature") {
        return {
          kind: "property",
          name: m.getName(),
          type: m.getTypeNode() ? m.getTypeNode().getText() : null,
          optional: m.hasQuestionToken(),
        };
      }
      if (kind === "MethodSignature") {
        return {
          kind: "method",
          name: m.getName(),
          params: m.getParameters().map(paramText),
          return_type: m.getReturnTypeNode() ? m.getReturnTypeNode().getText() : null,
        };
      }
      return { kind: kind.toLowerCase(), name: m.getText().slice(0, 80) };
    }),
  };
}

function extractTypeAlias(decl) {
  return {
    name: decl.getName(),
    definition: decl.getTypeNode() ? decl.getTypeNode().getText().slice(0, 800) : null,
  };
}

function extractEnum(decl) {
  return {
    name: decl.getName(),
    members: decl.getMembers().map((m) => ({
      name: m.getName(),
      value: m.getValue() !== undefined ? String(m.getValue()) : null,
    })),
  };
}

// Detect zustand-style stores: `export const useFoo = create<X>(...)`
// We dig into the `create(...)` call's first argument, which is a
// `(set, get) => ({ ...store body... })` arrow function. Extract the
// top-level keys of the returned object literal.
function extractZustandStore(varDecl) {
  const init = varDecl.getInitializer();
  if (!init) return null;

  // Need a CallExpression where the callee is `create` (or `create<T>`).
  if (init.getKind() !== SyntaxKind.CallExpression) return null;
  const callee = init.getExpression();
  const calleeName = callee.getKind() === SyntaxKind.Identifier
    ? callee.getText()
    : null;
  // ts-morph splits the generic type args off into a separate
  // `typeArguments` collection. The callee identifier is just `create`.
  if (calleeName !== "create") return null;

  const args = init.getArguments();
  if (args.length === 0) return null;

  // First argument is typically an arrow function. Walk to its return value.
  const arg = args[0];
  let body;
  if (arg.getKind() === SyntaxKind.ArrowFunction || arg.getKind() === SyntaxKind.FunctionExpression) {
    const fnBody = arg.getBody();
    if (!fnBody) return null;
    if (fnBody.getKind() === SyntaxKind.ParenthesizedExpression) {
      body = fnBody.getExpression();
    } else if (fnBody.getKind() === SyntaxKind.ObjectLiteralExpression) {
      body = fnBody;
    } else {
      // Block: look for `return { ... }`
      const ret = fnBody.getStatements().find((s) => s.getKind() === SyntaxKind.ReturnStatement);
      if (ret) body = ret.getExpression();
    }
  } else if (arg.getKind() === SyntaxKind.ObjectLiteralExpression) {
    body = arg;
  }
  if (!body || body.getKind() !== SyntaxKind.ObjectLiteralExpression) return null;

  const members = body.getProperties().map((p) => {
    if (p.getName) return p.getName();
    return p.getText().split(/[:\(]/)[0].trim();
  }).filter(Boolean);

  // Pull the type-arg if any: `create<AuthState>(...)` — ts-morph
  // exposes generic type arguments as a separate collection on the
  // call expression.
  let typeArg = null;
  const typeArgs = init.getTypeArguments ? init.getTypeArguments() : [];
  if (typeArgs.length > 0) {
    typeArg = typeArgs[0].getText();
  }

  return {
    name: varDecl.getName(),
    type_arg: typeArg,
    members,
  };
}

function detectFrameworks(imports) {
  const hints = new Set();
  for (const imp of imports) {
    const m = (imp.module || "").toLowerCase();
    if (!m) continue;
    if (m === "react" || m.startsWith("react/") || m.startsWith("react-")) hints.add("react");
    if (m === "vue" || m.startsWith("vue/") || m.startsWith("vue-")) hints.add("vue");
    if (m.startsWith("@angular/")) hints.add("angular");
    if (m === "svelte" || m.startsWith("svelte/")) hints.add("svelte");
    if (m === "zustand" || m.startsWith("zustand/")) hints.add("zustand");
    if (m === "@reduxjs/toolkit" || m === "redux") hints.add("redux");
    if (m.startsWith("react-router")) hints.add("react-router");
    if (m === "axios" || m.startsWith("axios/")) hints.add("axios");
  }
  return [...hints];
}

// ── per-file processing ────────────────────────────────────────────

for (const sourceFile of project.getSourceFiles()) {
  const abs = sourceFile.getFilePath();
  const rel = path.relative(WORKSPACE, abs);

  let fileDoc;
  try {
    const imports = sourceFile.getImportDeclarations().map((imp) => ({
      module: imp.getModuleSpecifierValue(),
      names: [
        ...imp.getNamedImports().map((n) => n.getName()),
        ...(imp.getDefaultImport() ? [imp.getDefaultImport().getText()] : []),
        ...(imp.getNamespaceImport() ? ["* as " + imp.getNamespaceImport().getText()] : []),
      ],
    }));

    const exports = [];
    for (const fn of sourceFile.getFunctions()) {
      if (!fn.isExported()) continue;
      exports.push(extractFunction(fn));
    }
    for (const cls of sourceFile.getClasses()) {
      if (!cls.isExported()) continue;
      exports.push(extractClass(cls));
    }
    for (const en of sourceFile.getEnums()) {
      if (!en.isExported()) continue;
      exports.push({ kind: "enum", ...extractEnum(en) });
    }
    // Variable statements: capture exported `const/let/var` names + types.
    for (const vs of sourceFile.getVariableStatements()) {
      if (!vs.isExported()) continue;
      for (const decl of vs.getDeclarations()) {
        const name = decl.getName();
        const init = decl.getInitializer();
        const typeNode = decl.getTypeNode();
        exports.push({
          kind: "const",
          name,
          default: false,
          type: typeNode ? typeNode.getText() : null,
          initializer_kind: init ? init.getKindName() : null,
        });
      }
    }

    const interfaces = sourceFile.getInterfaces()
      .filter((i) => i.isExported())
      .map(extractInterface);

    const types = sourceFile.getTypeAliases()
      .filter((t) => t.isExported())
      .map(extractTypeAlias);

    // Zustand stores live as exported variable decls; pull them out.
    const stores = [];
    for (const vs of sourceFile.getVariableStatements()) {
      if (!vs.isExported()) continue;
      for (const decl of vs.getDeclarations()) {
        const store = extractZustandStore(decl);
        if (store) stores.push(store);
      }
    }

    detectFrameworks(imports).forEach((h) => out.framework_hints.add(h));

    fileDoc = { imports, exports, interfaces, types, stores };
  } catch (e) {
    fileDoc = {
      imports: [],
      exports: [],
      interfaces: [],
      types: [],
      stores: [],
      _parse_error: `${e.name}: ${e.message}`,
    };
  }

  out.files[rel] = fileDoc;
}

out.framework_hints = [...out.framework_hints].sort();
process.stdout.write(JSON.stringify(out, null, 2));
