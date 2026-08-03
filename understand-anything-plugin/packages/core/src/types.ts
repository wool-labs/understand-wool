// Node types (27 total: 5 code + 8 non-code + 3 domain + 5 knowledge + 6 design)
export type NodeType =
  | "file" | "function" | "class" | "module" | "concept"
  | "config" | "document" | "service" | "table" | "endpoint"
  | "pipeline" | "schema" | "resource"
  | "domain" | "flow" | "step"
  | "article" | "entity" | "topic" | "claim" | "source"
  | "page" | "screen" | "component" | "componentSet" | "instance" | "token";

// Edge types (38 total in 9 categories: Structural, Behavioral, Data flow, Dependencies, Semantic, Infrastructure/Schema, Domain, Knowledge, Design)
export type EdgeType =
  | "imports" | "exports" | "contains" | "inherits" | "implements"  // Structural
  | "calls" | "subscribes" | "publishes" | "middleware"              // Behavioral
  | "reads_from" | "writes_to" | "transforms" | "validates"         // Data flow
  | "depends_on" | "tested_by" | "configures"                       // Dependencies
  | "related" | "similar_to"                                         // Semantic
  | "deploys" | "serves" | "provisions" | "triggers"                // Infrastructure
  | "migrates" | "documents" | "routes" | "defines_schema"          // Schema/Data
  | "contains_flow" | "flow_step" | "cross_domain"                  // Domain
  | "cites" | "contradicts" | "builds_on" | "exemplifies" | "categorized_under" | "authored_by" // Knowledge
  | "instance_of" | "variant_of" | "uses_token"; // Design

// Optional knowledge metadata for article/entity/topic/claim/source nodes
export interface KnowledgeMeta {
  wikilinks?: string[];
  backlinks?: string[];
  category?: string;
  content?: string;
}

// Optional domain metadata for domain/flow/step nodes
export interface DomainMeta {
  entities?: string[];
  businessRules?: string[];
  crossDomainInteractions?: string[];
  entryPoint?: string;
  entryType?: "http" | "cli" | "event" | "cron" | "manual";
}

// Optional Figma metadata for page/screen/component/componentSet/instance/token nodes
export interface FigmaMeta {
  fileKey?: string;
  nodeId?: string;            // Figma node id, e.g. "1:23"
  figmaType?: string;         // FRAME | COMPONENT | COMPONENT_SET | INSTANCE | TEXT ...
  thumbnailUrl?: string;      // lazily filled from GET /v1/images
  dimensions?: { width: number; height: number };
  tokenKind?: "color" | "type" | "spacing" | "effect" | "grid";
  tokenValue?: string;        // e.g. "#0A84FF", "16px"
  prototypeTargets?: string[]; // roadmap B — recorded now, edges later
  componentKey?: string;       // roadmap C — recorded now
}

// GraphNode with 27 types: 5 code + 8 non-code + 3 domain + 5 knowledge + 6 design
export interface GraphNode {
  id: string;
  type: NodeType;
  name: string;
  filePath?: string;
  lineRange?: [number, number];
  summary: string;
  tags: string[];
  complexity: "simple" | "moderate" | "complex";
  languageNotes?: string;
  domainMeta?: DomainMeta;
  knowledgeMeta?: KnowledgeMeta;
  figmaMeta?: FigmaMeta;
}

// GraphEdge with rich relationship modeling
export interface GraphEdge {
  source: string;
  target: string;
  type: EdgeType;
  direction: "forward" | "backward" | "bidirectional";
  description?: string;
  weight: number; // 0-1
}

// Layer (logical grouping)
export interface Layer {
  id: string;
  name: string;
  description: string;
  nodeIds: string[];
}

// TourStep (for learn mode)
export interface TourStep {
  order: number;
  title: string;
  description: string;
  nodeIds: string[];
  languageLesson?: string;
}

// ProjectMeta
export interface ProjectMeta {
  name: string;
  languages: string[];
  frameworks: string[];
  description: string;
  analyzedAt: string;
  gitCommitHash: string;
}

// Root KnowledgeGraph
export interface KnowledgeGraph {
  version: string;
  kind?: "codebase" | "knowledge" | "design";
  project: ProjectMeta;
  nodes: GraphNode[];
  edges: GraphEdge[];
  layers: Layer[];
  tour: TourStep[];
}

// Theme configuration (for dashboard customization)
export interface ThemeConfig {
  presetId: string;
  accentId: string;
}

// AnalysisMeta (for persistence)
export interface AnalysisMeta {
  lastAnalyzedAt: string;
  gitCommitHash: string;
  version: string;
  analyzedFiles: number;
  theme?: ThemeConfig;
}

// Project config (for auto-update opt-in and language preference)
export interface ProjectConfig {
  autoUpdate: boolean;
  outputLanguage?: string;
}

// Non-code structural sub-interfaces
export interface SectionInfo {
  name: string;
  level: number;
  lineRange: [number, number];
}

export interface DefinitionInfo {
  name: string;
  /** Parser-reported definition kind. Known values: "table", "view", "index", "message", "enum", "type", "input", "interface", "union", "scalar", "variable", "output", "resource", "data", "section", "target", "stage" */
  kind: string;
  lineRange: [number, number];
  fields: string[];
}

export interface ServiceInfo {
  name: string;
  image?: string;
  ports: number[];
  lineRange?: [number, number];
}

export interface EndpointInfo {
  method?: string;
  path: string;
  lineRange: [number, number];
}

export interface StepInfo {
  name: string;
  lineRange: [number, number];
}

export interface ResourceInfo {
  name: string;
  kind: string;
  lineRange: [number, number];
}

export interface ReferenceResolution {
  source: string;
  target: string;
  referenceType: string; // "file", "image", "schema", "service"
  line?: number;
}

/**
 * One definition — function, method, closure, or class — with its position in the
 * file's lexical nesting.
 *
 * This exists because `functions[]` and `classes[]` cannot answer "who called
 * this?". `functions[]` holds only module-level defs; a method reaches the graph
 * as a bare name string inside `classes[].methods`, and a nested closure appears
 * nowhere at all. Measured on the `wool` corpus, that blind spot cost 90% of the
 * call graph: 73% of call sites sit inside methods and 9% inside closures, and
 * neither could be named as an edge endpoint.
 *
 * `qualname` is the dotted path from file scope (`Class.method`,
 * `outer.inner`, `Class.method.closure`) and is what a graph node id is built
 * from, so it must be stable and unambiguous within a file.
 */
export interface SymbolInfo {
  qualname: string;
  name: string;
  kind: "function" | "method" | "closure" | "class";
  parentQualname?: string;
  lineRange: [number, number];
  params: string[];
  returnType?: string;
  /** Lexical nesting depth; 0 is module scope. */
  depth: number;
  isAsync?: boolean;
  exported?: boolean;
  /**
   * The body contains no executable statements — `...`, `pass`, or
   * `raise NotImplementedError`, docstring aside.
   *
   * Load-bearing for call resolution: a stub cannot be a call target at
   * runtime. When several candidates share a name and exactly one has a real
   * body, binding to it is deterministic rather than a guess — which is the
   * common Protocol-declaration / single-implementation shape, and also covers
   * abstract bases and leftover `@overload` signatures.
   */
  isStub?: boolean;
}

// Plugin interfaces
export interface StructuralAnalysis {
  functions: Array<{ name: string; lineRange: [number, number]; params: string[]; returnType?: string }>;
  classes: Array<{ name: string; lineRange: [number, number]; methods: string[]; properties: string[] }>;
  imports: Array<{ source: string; specifiers: string[]; lineNumber: number }>;
  exports: Array<{ name: string; lineNumber: number; isDefault?: boolean }>;
  // Non-code structural data (all optional for backward compat)
  sections?: SectionInfo[];
  definitions?: DefinitionInfo[];
  services?: ServiceInfo[];
  endpoints?: EndpointInfo[];
  steps?: StepInfo[];
  resources?: ResourceInfo[];
  /**
   * Flat, source-ordered index of every definition in the file, nested ones
   * included. A superset of `functions[]` + `classes[]`, added rather than
   * folded into them: `fingerprint.ts` maps `functions[]` straight into
   * `FunctionFingerprint[]`, so growing that array would churn every Python
   * fingerprint and can escalate a run to FULL_UPDATE via `change-classifier`.
   * Optional so extractors that have not been upgraded keep working unchanged.
   */
  symbols?: SymbolInfo[];
}

export interface ImportResolution {
  source: string;
  resolvedPath: string;
  specifiers: string[];
}

/**
 * One call site.
 *
 * `caller` is the **dotted qualified name of the innermost enclosing named
 * scope, rooted at file scope** — `DispatchSession._schedule_worker._start._run`,
 * not `_run`. Extractors that have not been upgraded still emit the simple name;
 * consumers must degrade gracefully rather than assume qualification.
 *
 * `callee` stays the raw source text of the callee expression. The decomposed
 * `calleeName`/`calleeReceiver` pair is what a resolver should use — `callee`
 * alone cannot distinguish `foo()` from `x.foo()`, and those resolve by
 * completely different rules.
 */
export interface CallGraphEntry {
  caller: string;
  callee: string;
  lineNumber: number;
  /** Innermost simple name of the caller, e.g. "_run". */
  callerName?: string;
  callerKind?: SymbolInfo["kind"];
  /** Last dotted segment of the callee, e.g. "routine_scope". */
  calleeName?: string;
  /** Receiver expression for an attribute call: "self", "cls", "os.path". */
  calleeReceiver?: string;
}

export interface AnalyzerPlugin {
  name: string;
  languages: string[];
  analyzeFile(filePath: string, content: string): StructuralAnalysis;
  resolveImports?(filePath: string, content: string): ImportResolution[];
  extractCallGraph?(filePath: string, content: string): CallGraphEntry[];
  extractReferences?(filePath: string, content: string): ReferenceResolution[];
  /**
   * Optional single-parse fast path returning both structure and call graph.
   * Plugins that parse source (e.g. tree-sitter) can implement this to avoid
   * parsing the same file twice when a caller needs both. Output must equal
   * `analyzeFile` + `extractCallGraph` called separately.
   */
  analyzeFileFull?(
    filePath: string,
    content: string,
  ): { structure: StructuralAnalysis; callGraph: CallGraphEntry[] };
}
