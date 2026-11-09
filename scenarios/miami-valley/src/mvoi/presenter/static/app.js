// Presenter page: MSAL sign-in, the Fabric Embed SDK (preview) for the live
// dashboard, and a chat panel that posts questions to the local server.
import { PublicClientApplication, InteractionRequiredAuthError } from "https://cdn.jsdelivr.net/npm/@azure/msal-browser@3.27.0/+esm";
import { EmbedManager, KQLDashboardEmbedClient } from "https://cdn.jsdelivr.net/npm/@microsoft/fabric-embed@0.1.0/+esm";
import { marked } from "https://cdn.jsdelivr.net/npm/marked@12.0.2/+esm";
import DOMPurify from "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/+esm";

const FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default";
const $ = (id) => document.getElementById(id);

function setStatus(text, isError = false) {
  const el = $("embed-status");
  el.hidden = !text;
  el.textContent = text || "";
  el.classList.toggle("error", isError);
}

async function main() {
  const cfg = await (await fetch("/api/config")).json();
  $("agent-name").textContent = cfg.agentName;

  const msal = new PublicClientApplication({
    auth: {
      clientId: cfg.clientId,
      authority: `https://login.microsoftonline.com/${cfg.tenantId}`,
      redirectUri: `${location.origin}/`,
    },
    cache: { cacheLocation: "localStorage" },
  });
  await msal.initialize();
  const redirect = await msal.handleRedirectPromise();
  const account = redirect?.account ?? msal.getAllAccounts()[0];
  if (!account) {
    setStatus("Sign in with your Fabric account to load the live dashboard.");
    const btn = $("signin");
    btn.hidden = false;
    btn.onclick = () => msal.loginRedirect({ scopes: [FABRIC_SCOPE] });
    return;
  }
  msal.setActiveAccount(account);

  async function acquireToken(scopes) {
    const request = { scopes: scopes?.length ? scopes : [FABRIC_SCOPE], account };
    try {
      return (await msal.acquireTokenSilent(request)).accessToken;
    } catch (err) {
      if (err instanceof InteractionRequiredAuthError) {
        await msal.acquireTokenRedirect(request);
      }
      throw err;
    }
  }

  setStatus("Loading the live dashboard…");
  const manager = new EmbedManager({ embedClientClasses: [KQLDashboardEmbedClient] });
  const client = manager.embed($("embed"), {
    itemType: "KQLDashboard",
    workspaceId: cfg.workspaceId,
    itemId: cfg.dashboardId,
    accessToken: { token: await acquireToken() },
    eventHooks: {
      accessTokenProvider: { callback: async ({ scopes }) => ({ token: await acquireToken(scopes) }) },
    },
  });
  client.on("rendered", () => setStatus(""));
  client.on("error", (e) => setStatus(`Dashboard error: ${e.errorCode}${e.message ? ` — ${e.message}` : ""}`, true));
}

// ---- chat
function addMessage(kind, html) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.innerHTML = html;
  $("messages").append(el);
  el.scrollIntoView({ behavior: "smooth", block: "end" });
  return el;
}

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

async function ask(question) {
  const button = $("ask").querySelector("button");
  button.disabled = true;
  addMessage("q", escapeHtml(question));
  const pending = addMessage("a wait", "Asking the data agent…");
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    pending.className = "msg a";
    pending.innerHTML = DOMPurify.sanitize(marked.parse(body.answer));
  } catch (err) {
    pending.className = "msg a err";
    pending.textContent = `The data agent could not answer: ${err.message}`;
  } finally {
    button.disabled = false;
  }
}

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (!q) return;
  $("question").value = "";
  ask(q);
});
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("ask").requestSubmit();
  }
});
document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => ask(chip.textContent)));

main().catch((err) => setStatus(`Could not start: ${err.message}`, true));
