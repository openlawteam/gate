# Example App Codebase Standards — Gate Review Reference

Compiled from `.cursor/rules/*.mdc`. Reframed for code review: flag violations, cite specifics.

---

## 1. Architecture & Layer Boundaries

### Layer Diagram

```
Routes (src/app/api/)  →  lib/ai  →  lib/services  →  db
                                ↘         ↓
                            lib/integrations
```

**FLAG** if a PR introduces:
- An API route (`src/app/api/`) that imports from `@/db` directly — routes must delegate to services
- A service (`src/lib/services/`) that imports from `lib/ai` (exception: shared types like `AgentDefinition`)
- A `db` module that imports from `lib/ai` or `lib/services` — db is the bottom layer
- Any upward dependency that violates the direction shown above

### Key Directories

| Directory | Purpose |
|-----------|---------|
| `src/app/api/` | API routes — `apiSuccess()` + `handleRouteError()` |
| `src/app/chat/` | Chat UI — orchestrator, hooks, components, settings |
| `src/lib/ai/` | AI tools, system prompt, agent definitions, widgets |
| `src/lib/services/` | Business logic — domain subdirectories with barrel exports |
| `src/lib/integrations/` | OAuth config, provider API clients |
| `src/db/` | Drizzle schema, repositories, client |
| `src/types/` | Shared types used by 3+ modules |
| `src/contexts/` | React contexts (Auth, Team, Locale, Profile) |
| `src/components/` | Shared UI (Header, posts, ui primitives) |
| `extension/` | Chrome extension (vanilla JS, Manifest V3) |

**FLAG** if a file is placed in the wrong directory for its purpose.

### Module Boundaries — Types Organization

| Location | Use when |
|----------|----------|
| `src/types/` | Shared across 3+ modules |
| Co-located (e.g. `lib/services/foo/types.ts`) | Used within that service only |
| `db/schema/` and `db/types/` | Database-derived types |

**FLAG** if the same type is defined in two places — pick one location and import.
**FLAG** if a type is promoted to `src/types/` but only used by 1-2 consumers.

---

## 2. API Route Standards

### Required Skeleton

Every API route must follow this pattern:

```typescript
import { apiSuccess, handleRouteError, invalidJsonResponse, validationErrorResponse } from "@/lib/api/routeError";
import { requireAuth } from "@/lib/auth/helpers";

export const runtime = "nodejs";

export async function POST(req: Request): Promise<NextResponse> {
  const operation = "POST /api/your-route";

  let body;
  try {
    const rawBody = await req.json();
    const parsed = MySchema.safeParse(rawBody);
    if (!parsed.success) return validationErrorResponse(parsed.error.issues, operation);
    body = parsed.data;
  } catch {
    return invalidJsonResponse(operation);
  }

  try {
    const user = await requireAuth();
    const result = await myService(body, user.id);
    return apiSuccess(result, 201);
  } catch (error) {
    return handleRouteError(error, operation);
  }
}
```

**FLAG** if a new route handler:
- Omits `requireAuth()` (unless the route explicitly supports unauthenticated access)
- Uses `getCurrentUser()` where `requireAuth()` should be (getCurrentUser is for optional auth)
- Omits `export const runtime = "nodejs"` when using DB or Node APIs
- Omits `const operation = "METHOD /api/path"`
- Uses raw `NextResponse.json()` instead of response helpers
- Omits `handleRouteError(error, operation)` in the catch block
- Imports validation helpers from `@/lib/validators/common` instead of `@/lib/api/routeError`

### API Response Envelope

**FLAG** if a route returns `NextResponse.json()` directly for success responses — use `apiSuccess()`.
**FLAG** any call to `apiSuccess({ data: ... })` — this causes double-wrapping.
**FLAG** if a route is migrated to `apiSuccess` but its callers still read the response without `unwrap()`.

SWR hooks consuming `apiSuccess` routes should type the envelope:
```typescript
const { data } = useSWR<{ data: { code: string } }>("/api/referral/code");
const code = data?.data?.code;
```

### Response Helpers

| Helper | Status | When |
|--------|--------|------|
| `apiSuccess(data, status?)` | 200/201 | All success responses |
| `handleRouteError(error, op)` | varies | Catch blocks |
| `validationErrorResponse(issues, op)` | 400 | Zod safeParse failures |
| `invalidJsonResponse(op)` | 400 | req.json() throws |
| `badRequestResponse(msg, op)` | 400 | General bad requests |
| `unauthorizedResponse(msg, op)` | 401 | Auth failures |
| `paymentRequiredResponse(msg, op)` | 402 | Payment required |
| `forbiddenResponse(msg, op)` | 403 | Insufficient permissions |
| `notFoundResponse(msg, op)` | 404 | Resource not found |
| `conflictResponse(msg, op)` | 409 | Duplicate resources |
| `rateLimitResponse(msg, op)` | 429 | Rate limit exceeded |
| `serverErrorResponse(msg, op)` | 500 | Server errors |

---

## 3. Code Quality

### File Size Limits

- **Target**: 350 lines per file (soft limit)
- **Hard ceiling**: 600 lines per file (ESLint error)
- **Functions/Components**: Max 450 lines

**FLAG** any new or modified file exceeding 350 lines. Error if exceeding 600.

Split patterns:

| File type | Split pattern |
|-----------|---------------|
| Component | Extract sub-components (`FooBar.tsx` + `FooBarForm.tsx`) |
| Hook | Extract into `useFooState.ts` |
| Service | Extract helpers into `fooHelpers.ts`, types into `fooTypes.ts` |
| Tool/agent | Extract schemas into `fooSchema.ts`, handlers into `fooHandlers.ts` |

Keep original as barrel with re-exports so import paths don't break.

### Unused Variables & Imports

**FLAG** unused imports or variables. Fix: remove or prefix with underscore (`_unusedVar`).

### Console Statements

**FLAG** `console.log()` statements. Only `console.warn()` and `console.error()` are acceptable.

### React Hook Dependencies

**FLAG** if a `useEffect`/`useMemo`/`useCallback` has stale or missing dependencies, especially objects recreated every render.

### Next.js Images

**FLAG** usage of `<img>` tags — use `next/image` `<Image>` component instead.

### Error Handling in Catch Blocks

TypeScript `strict: true` makes catch variables `unknown`. **FLAG** any catch block that accesses `.message` or other properties without an `instanceof Error` check.

```typescript
// ❌ BAD
catch (error) { return res.json({ error: error.message }); }

// ✅ GOOD
catch (error: unknown) {
  const message = error instanceof Error ? error.message : "Unknown error";
  return res.json({ error: message });
}
```

### Lint Suppression

**FLAG** usage of `// eslint-disable-next-line`, `// @ts-ignore`, or `// @ts-expect-error` unless accompanied by a comment explaining why the suppression is necessary.

### Environment Variables

**FLAG** any use of `process.env` directly. All env access must go through `@/lib/config/env` (Zod-validated).

---

## 4. Naming Conventions

### File Naming

| Type | Convention | Example |
|------|-----------|---------|
| React components | PascalCase | `ChatInput.tsx` |
| shadcn/ui primitives | kebab-case | `dropdown-menu.tsx` |
| ToolBadge renderers | kebab-case | `gmail-tools.tsx` |
| Hooks | camelCase + `use` prefix | `useChatEffects.ts` |
| Services | camelCase + `Service` suffix | `ragService.ts` |
| Clients | camelCase + `Client` suffix | `mercuryClient.ts` |
| Helpers | camelCase + `Helpers` suffix | `chatInputHelpers.ts` |
| Utils | camelCase + `Utils` suffix | `dateUtils.ts` |
| Repositories | camelCase + `Repo` suffix | `conversationRepo.ts` |
| Types | camelCase/PascalCase + `Types` suffix | `ragTypes.ts` |
| Constants | camelCase + `Constants` suffix | `chatConstants.ts` |

**FLAG** files that don't match their directory's convention.
**FLAG** helpers with dashes or PascalCase prefix (`ChatHistoryBoard-helpers.tsx` → `chatHistoryBoardHelpers.tsx`).

### Suffix Semantics

| Suffix | Purpose | Imports DB? | Calls external APIs? |
|--------|---------|-------------|---------------------|
| `Service` | Business logic | Yes | Sometimes |
| `Client` | External API wrapper | No | Yes |
| `Helpers` | Domain-specific logic | Maybe | No |
| `Utils` | Pure functions, no domain coupling | No | No |

---

## 5. Data Access Patterns

### Two Approaches

1. **Direct `db` + schema** — default for most services
2. **Repository layer** — use when a repo exists for the table

Existing repositories: `conversationRepo`, `userRepo`, `workflowRepo`, `gamificationRepo`, `agentMemoryRepo`, `chatFolderRepo`, `healingIssueRepo`, `publishedPostRepo`, `ragChunkRepo`, `referralRepo`, `smsMessageRepo`, `userFileRepo`, `userIntegrationRepo`

**FLAG** if a service writes a query that duplicates an existing repository method.
**FLAG** if an API route imports `@/db` directly — delegate to services.
**FLAG** if a new repo is created for a single service — that's overengineering.

### Barrel Exports

Service directories with 3+ files must have an `index.ts` barrel that re-exports the public API.
**FLAG** if a file split doesn't update the barrel file.

---

## 6. State Management

Three patterns coexist:

| Pattern | Use when |
|---------|----------|
| **React Context** | App-wide identity/config that rarely changes (auth, locale, theme) |
| **Nanostores** | Synchronous cross-framework reads (rare — currently only `teamsStore`) |
| **SWR** | Any data fetched from API routes |

**FLAG** if new server-derived state is managed with Context instead of SWR.
**FLAG** if a new nanostore is added without justification for synchronous reads.

SWR keys are centralized in `src/lib/swr.ts`. **FLAG** if a new SWR hook uses an inline key instead of adding to `swr.ts`.

---

## 7. Refactoring Safety

### Migration Ordering

**FLAG** if a PR modifies a Drizzle schema file (`src/db/schema/`) AND references the new column in application code, but no migration file is included. This will cause runtime failures.

Correct sequence: schema → `drizzle-kit generate` → review SQL → `drizzle-kit migrate` → THEN use new column.

### File Split Verification

**FLAG** if a PR splits a file but:
- The barrel file is missing re-exports (consumers get `undefined`)
- Relative import paths are wrong after moving to a sibling
- Type parameters or generics were dropped during extraction

### Scope Discipline

**FLAG** if the PR modifies files that aren't related to the stated change — risk of unintended regressions.

---

## 8. Chat & AI Patterns

### Tool Registration (5 steps — all required in same PR)

1. Create tool file in `src/lib/ai/{domain}Tools/`
2. Add tool names to `src/lib/ai/toolInfra/toolNames.ts`
3. Register in `src/lib/ai/toolInfra/toolFactory.ts` (base) or `toolFactoryIntegrations.ts` (integration)
4. Add tool config (icon, color) in `src/app/chat/components/toolConfigMap/*.ts`
5. Add ToolBadge renderer in `src/app/chat/components/ToolBadge/`

**FLAG** if a PR adds a new tool file matching `src/lib/ai/*Tools/*.ts` but does NOT modify `toolNames.ts`, `toolFactory.ts`, or `toolConfigMap`. A tool without these registrations will silently never load.

### Widget Registration

1. Create widget component in `src/app/chat/components/widgets/`
2. Add widget tool in `src/lib/ai/widgetInfra/widgetTools.ts`
3. Add `WidgetDefinition` to the appropriate `registry-*.ts` file

### Integration Registration

1. Add provider to `IntegrationProvider` union in `src/types/integrations.ts`
2. Add OAuth config in `src/lib/integrations/oauth-config.ts`
3. Create auth module `src/lib/integrations/providers/{name}-auth.ts`
4. Register tools in `toolFactoryIntegrations.ts`

**FLAG** if a new integration is added without all registration steps.

### Self-Awareness Updates

**FLAG** if the PR adds a new tool, visualization type, agent, or integration but does NOT update `src/lib/ai/systemPromptSelfAwareness.ts`.

---

## 9. Component Organization

Chat components live in `src/app/chat/components/` with domain subdirectories:

`anki/`, `chatHistory/`, `ChatMessage/`, `connectCards/`, `conversationSidebar/`, `flows/`, `settings/`, `tasks/`, `ToolBadge/`, `visualization/`, `welcome/`, `widgets/`, `markdown/`, `sharing/`, `timeline/`

**FLAG** if a new component is placed at the wrong level (domain component at top level, or shared component buried in a domain folder).

### Settings — Two Directories

| Path | Purpose |
|------|---------|
| `src/app/chat/settings/` | Page route — tab sections, integration cards |
| `src/app/chat/components/settings/` | Modal settings boards from chat UI |

**FLAG** if a settings component is in the wrong directory for its purpose.

---

## 10. Commit & Debug Hygiene

### Conventional Commits

Format: `type(scope): description`

Types: `feat`, `fix`, `refactor`, `chore`, `docs`, `test`, `perf`

**FLAG** vague commit messages like "Small changes", "Fixes", "Updates", "WIP".

### Debug Code

**FLAG** any of the following in code destined for `main`:
- Temporary API endpoints (`/api/debug/...`)
- Auth removed "for debugging"
- `console.log` statements added for investigation
- Comments like "DELETE THIS FILE" or "Remove after debugging"

### Sensitive Data in Logs

**FLAG** logging of credentials, tokens, client IDs, redirect URIs, or full request bodies.

---

## 11. Package Manager

This project uses **npm** exclusively.

**FLAG** introduction of `pnpm-lock.yaml` or `yarn.lock`.
**FLAG** usage of `pnpm` or `yarn` commands.

---

## 12. Extension Patterns

*(Only relevant when files in `extension/` are changed)*

### Module Structure

| File | Role |
|------|------|
| `service-worker.js` | Orchestrator: message routing, tab management |
| `sw-site-recon.js` | Site recon handler |
| `sw-fix-error.js` | Error fix prompt builder |
| Content scripts (`content-*.js`) | DOM interaction modules sharing `window.__app` namespace |

### Adding New Commands

- DOM command: handler in `content-dom-commands.js` or `content-input-commands.js` → register in `COMMANDS` table in `content-script.js` → add to `forwardToContentScript` switch in `service-worker.js`
- Service worker command: add case to `handleExternalMessage` switch

**FLAG** if a new content script is added but not registered in both `manifest.json` content_scripts AND `CONTENT_SCRIPT_FILES` in `service-worker.js`.
