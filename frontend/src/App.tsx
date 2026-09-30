import ReactMarkdown from "react-markdown";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Activity,
  Bell,
  ArrowUp,
  ArrowUpRight,
  Download,
  FileCheck2,
  Layers,
  Menu,
  Orbit,
  PanelRight,
  Plus,
  Repeat2,
  Search,
  Settings2,
  SlidersHorizontal,
  Square,
  Wallet,
  X,
} from "lucide-react";
import { useMachine, type Machine } from "./api/useMachine";
import type { Agent, Role } from "./api/contracts";
import { Avatar, Alert, Badge, Button, Empty, Spinner } from "./components/ui";
import { ConversationCard } from "./components/ConversationCard";
import { SidePanel } from "./components/SidePanel";
import { downloadJson, short, time } from "./lib/format";

const roles: { id: Role; name: string; description: string }[] = [
  {
    id: "alpha",
    name: "Alpha",
    description: "Clarify your objectives and compare allocations.",
  },
  {
    id: "vault",
    name: "Vault",
    description: "Review your treasury, approvals and execution.",
  },
  {
    id: "watch",
    name: "Watch",
    description: "Follow changes in positions, liquidity and risk.",
  },
];
function AgentEditor({
  agent,
  m,
  onClose,
  onSaved,
  onArchived,
  initialRole,
}: {
  initialRole: Role;
  agent?: Agent;
  m: Machine;
  onClose: () => void;
  onSaved: (a: Agent) => void;
  onArchived: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(agent?.name || "");
  const [role, setRole] = useState<Role>(agent?.role || initialRole);
  const [instructions, setInstructions] = useState(agent?.instructions || "");
  const [confirmArchive, setConfirmArchive] = useState(false);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    dialog.current?.showModal();
    dialog.current?.querySelector("input")?.focus();
    return () => {
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="agent-dialog"
      onCancel={(e) => {
        e.preventDefault();
        if (!m.busy) onClose();
      }}
    >
      <header>
        <div>
          <span className="eyebrow">YOUR WORKSPACE</span>
          <h2>{agent ? "Agent settings" : "Create an agent"}</h2>
        </div>
        <button
          className="icon-button"
          aria-label="Close agent settings"
          onClick={onClose}
          disabled={!!m.busy}
        >
          <X />
        </button>
      </header>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const saved = await m.saveAgent(
            { name, role, instructions },
            agent?.id,
          );
          if (saved) onSaved(saved);
        }}
      >
        <label>
          Name
          <input
            autoFocus
            required
            maxLength={60}
            placeholder="Give your agent a name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <fieldset>
          <legend>Role</legend>
          <div className="role-options">
            {roles.map((r) => (
              <label key={r.id} className={role === r.id ? "selected" : ""}>
                <input
                  type="radio"
                  name="role"
                  value={r.id}
                  checked={role === r.id}
                  onChange={() => setRole(r.id)}
                />
                <Avatar role={r.id} />
                <span>
                  <strong>{r.name}</strong>
                  <small>{r.description}</small>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        <label>
          Instructions <span className="muted">Optional</span>
          <textarea
            rows={4}
            maxLength={4000}
            placeholder="How should this agent work with you?"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        </label>
        {m.error && <Alert tone="error">{m.error}</Alert>}
        <div className="dialog-actions">
          <Button secondary onClick={onClose} disabled={!!m.busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={!!m.busy || !name.trim()}>
            {m.busy || (agent ? "Save changes" : "Create agent")}
          </Button>
        </div>
      </form>
      {agent && (
        <div className="archive-agent">
          {confirmArchive ? (
            <>
              <p>
                Archive {agent.name}? The conversation will be retained in
                storage and removed from your agent list.
              </p>
              <Button
                className="danger-button"
                disabled={!!m.busy}
                onClick={async () => {
                  if (await m.archiveAgent(agent.id)) onArchived();
                }}
              >
                Confirm archive
              </Button>
              <Button secondary onClick={() => setConfirmArchive(false)}>
                Keep agent
              </Button>
            </>
          ) : (
            <button onClick={() => setConfirmArchive(true)}>
              Archive agent
            </button>
          )}
        </div>
      )}
    </dialog>
  );
}
export default function App() {
  const { agentId } = useParams();
  const m = useMachine(agentId);
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const panel = params.get("panel");
  const [initialRole, setInitialRole] = useState<Role>("alpha");
  const [menu, setMenu] = useState(false);
  const [editor, setEditor] = useState<"create" | "edit" | null>(null);
  const [text, setText] = useState("");
  const [search, setSearch] = useState("");
  const [theme, setThemeState] = useState(
    () => localStorage.getItem("machine:theme") || "system",
  );
  const agent = m.agents.data?.find((a) => a.id === agentId);
  const w = m.workspace.data;
  const messages = w?.messages || [];
  const jobs =
    w?.jobs.filter((j) => ["QUEUED", "RUNNING"].includes(j.status)) || [];
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const draft = useRef<Record<string, string>>({});
  const setTheme = (v: string) => {
    setThemeState(v);
    localStorage.setItem("machine:theme", v);
  };
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useEffect(() => {
    setText(draft.current[`${agentId}:${m.network}`] || "");
    stick.current = true;
  }, [agentId, m.network]);
  useEffect(() => {
    if (stick.current)
      scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [
    agentId,
    messages.at(-1)?.id,
    messages.at(-1)?.text,
    jobs.length,
    w?.graph?.id,
  ]);
  const choose = (id: string) => {
    setMenu(false);
    nav(`/agents/${id}?network=${m.network}`);
  };
  const openPanel = (p: string, editing = false) => {
    setMenu(false);
    const next = new URLSearchParams(params);
    next.set("panel", p);
    next.delete("strategy");
    if (editing) next.set("edit", "conditions");
    else next.delete("edit");
    setParams(next);
  };
  const closePanel = () => {
    const next = new URLSearchParams(params);
    next.delete("panel");
    next.delete("edit");
    next.delete("strategy");
    setParams(next);
  };
  const setDraft = (value: string) => {
    setText(value);
    draft.current[`${agentId}:${m.network}`] = value;
  };
  const send = async () => {
    if (!agent || !text.trim() || m.busy || jobs.length) return;
    const destination = `${agentId}:${m.network}`;
    const value = text.trim();
    stick.current = true;
    if (await m.message(agent.role, value)) {
      draft.current[destination] = "";
      setText((current) => (current.trim() === value ? "" : current));
    }
  };
  const create = (role: Role = "alpha") => {
    setInitialRole(role);
    m.setError(null);
    setEditor("create");
    setMenu(false);
  };
  const fatal =
    m.session.error || m.agents.error || (agentId ? m.workspace.error : null);
  return (
    <div className={`machine-app ${panel ? "has-panel" : ""}`}>
      <a href="#main-content" className="skip-link">
        Skip to conversation
      </a>
      {menu && (
        <button
          className="mobile-scrim"
          aria-label="Close navigation"
          onClick={() => setMenu(false)}
        />
      )}
      <aside
        className={`sidebar ${menu ? "is-open" : ""}`}
        aria-label="Workspace navigation"
      >
        <div className="brand">
          <Orbit size={27} />
          <span title="Finance AI Agent Tron">faat</span>
          <button
            className="icon-button mobile-only"
            aria-label="Close navigation"
            onClick={() => setMenu(false)}
          >
            <X />
          </button>
        </div>
        <button
          className="workspace-name"
          onClick={() => {
            nav(`/?network=${m.network}`);
            setMenu(false);
          }}
        >
          <span className="workspace-avatar">F</span>
          <span>
            Your workspace
            <small>
              {m.session.data?.authenticated
                ? short(m.session.data.wallet_address || "")
                : "Personal"}
            </small>
          </span>
        </button>
        <div className="sidebar-label">
          Agents <span>{m.agents.data?.length || 0}</span>
        </div>
        <Button
          secondary
          className="new-agent"
          onClick={() => create()}
          disabled={!m.session.isSuccess}
        >
          <Plus size={16} />
          Create agent
        </Button>
        {!!m.agents.data?.length && (
          <label className="agent-search">
            <Search size={14} />
            <input
              aria-label="Search agents"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search agents"
            />
          </label>
        )}
        <nav className="roster" aria-label="Agent conversations">
          {m.agents.data
            ?.filter((a) =>
              `${a.name} ${a.role}`
                .toLowerCase()
                .includes(search.toLowerCase()),
            )
            .map((a) => (
              <button
                key={a.id}
                className={`roster-item ${a.id === agentId ? "active" : ""}`}
                aria-current={a.id === agentId ? "page" : undefined}
                onClick={() => choose(a.id)}
              >
                <Avatar role={a.role} />
                <span className="roster-copy">
                  <strong>
                    {a.name}
                    {a.status === "WORKING" && <span className="working-dot" />}
                  </strong>
                  <small>{a.role[0].toUpperCase() + a.role.slice(1)}</small>
                  {a.last_message && (
                    <span>{a.last_message?.replace(/[*_`#]/g, "")}</span>
                  )}
                </span>
              </button>
            ))}
          {search &&
            !m.agents.data?.some((a) =>
              `${a.name} ${a.role}`
                .toLowerCase()
                .includes(search.toLowerCase()),
            ) && <p className="sidebar-note">No matching agents</p>}
        </nav>
        <div className="sidebar-label">Workspace</div>
        <nav className="capital-nav" aria-label="Capital tools">
          {[
            { id: "portfolio", label: "Portfolio", Icon: Layers },
            { id: "mandate", label: "Mandate", Icon: SlidersHorizontal },
            { id: "routines", label: "Routines", Icon: Repeat2 },
            { id: "notifications", label: "Notifications", Icon: Bell },
            { id: "activity", label: "Activity", Icon: Activity },
            { id: "evidence", label: "Run evidence", Icon: FileCheck2 },
          ].map(({ id, label, Icon }) => (
            <button
              key={id}
              className={panel === id ? "active" : ""}
              onClick={() => openPanel(id)}
            >
              <Icon size={17} />
              {label}
              {id === "notifications" &&
                (w?.notifications || []).some(
                  (n) => !n.read_at && !n.resolved_at,
                ) && (
                  <span className="notification-count">
                    {
                      (w?.notifications || []).filter(
                        (n) => !n.read_at && !n.resolved_at,
                      ).length
                    }
                  </span>
                )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <button className="wallet-nav" onClick={() => openPanel("wallet")}>
            <Wallet size={17} />
            {m.session.data?.authenticated
              ? short(m.session.data.wallet_address || "")
              : "Connect TronLink"}
            <ArrowUpRight size={16} />
          </button>
          <div className="account">
            <span className="workspace-avatar">F</span>
            <span>
              Workspace settings<small>Appearance & usage</small>
            </span>
            <button
              className="icon-button"
              aria-label="Workspace settings"
              onClick={() => openPanel("settings")}
            >
              <Settings2 size={17} />
            </button>
          </div>
        </div>
      </aside>
      <main className="conversation" id="main-content">
        <header className="chat-header">
          <button
            className="icon-button mobile-only"
            aria-label="Open navigation"
            onClick={() => setMenu(true)}
          >
            <Menu />
          </button>
          {agent ? (
            <button
              className="agent-identity"
              aria-label="Agent settings"
              onClick={() => {
                m.setError(null);
                setEditor("edit");
              }}
            >
              <Avatar role={agent.role} />
              <span>
                <strong>{agent.name}</strong>
                <small>
                  {jobs.length
                    ? "Working…"
                    : `${agent.role[0].toUpperCase() + agent.role.slice(1)} · Qwen3`}
                </small>
              </span>
            </button>
          ) : (
            <strong>Workspace</strong>
          )}
          <div className="header-tools">
            <label className="network-selector">
              <span className="sr-only">Network</span>
              <select
                aria-label="Network"
                value={m.network}
                onChange={(e) =>
                  m.changeNetwork(e.target.value as "nile" | "mainnet")
                }
                disabled={!!m.busy}
              >
                <option value="nile">Nile Testnet</option>
                <option value="mainnet">TRON Mainnet</option>
              </select>
            </label>
            <button
              className="icon-button notification-bell"
              aria-label={`Account notifications, ${(w?.notifications || []).filter((n) => !n.read_at && !n.resolved_at).length} unread`}
              onClick={() => openPanel("notifications")}
            >
              <Bell size={18} />
              {(w?.notifications || []).some(
                (n) => !n.read_at && !n.resolved_at,
              ) && <span className="notification-dot" />}
            </button>
            {agent && (
              <button
                className="icon-button"
                disabled={!messages.length}
                aria-label="Export conversation"
                onClick={() =>
                  downloadJson(
                    {
                      agent,
                      network: m.network,
                      exported_at: new Date().toISOString(),
                      messages,
                    },
                    `faat-${agent.id}.json`,
                  )
                }
              >
                <Download size={17} />
              </button>
            )}
            <button
              className="icon-button"
              aria-label={panel ? "Close details" : "Open portfolio"}
              onClick={() => (panel ? closePanel() : openPanel("portfolio"))}
            >
              <PanelRight size={19} />
            </button>
          </div>
        </header>
        <div className="global-notices">
          {m.pending && (
            <Alert>
              <div>{m.pendingMessage}</div>
              <button onClick={m.reconcile} disabled={!!m.busy}>
                {m.pendingStatus === "CONFIRMING"
                  ? "Check confirmation"
                  : "Check transaction status"}
              </button>
            </Alert>
          )}
          {m.error && !editor && (
            <Alert tone="error">
              <div>{m.error}</div>
              <button onClick={() => m.setError(null)}>Dismiss</button>
            </Alert>
          )}
          {m.notice && (
            <Alert>
              <div>{m.notice}</div>
              <button onClick={() => m.setNotice(null)}>Dismiss</button>
            </Alert>
          )}
          {m.workspace.isError && w && (
            <Alert tone="error">
              Connection interrupted. Showing the last loaded state.
            </Alert>
          )}
        </div>
        <div
          className="timeline-scroll"
          ref={scroller}
          onScroll={(e) => {
            const t = e.currentTarget;
            stick.current = t.scrollHeight - t.scrollTop - t.clientHeight < 100;
          }}
        >
          <div className="timeline">
            {m.session.isPending || m.agents.isLoading ? (
              <Spinner />
            ) : fatal && !w ? (
              <Empty title="Unable to load workspace">
                <span>{fatal.message}</span>
                <Button
                  onClick={() => {
                    m.session.refetch();
                    m.agents.refetch();
                    m.workspace.refetch();
                  }}
                >
                  Retry connection
                </Button>
              </Empty>
            ) : !agent ? (
              <div className="workspace-empty">
                <div className="empty-orbit">
                  <Orbit size={42} strokeWidth={1} />
                </div>
                <span className="eyebrow">YOUR AGENTS</span>
                <h1>
                  {agentId
                    ? "Agent unavailable"
                    : m.agents.data?.length
                      ? "Choose an agent"
                      : "Create your first agent"}
                </h1>
                <p>
                  {agentId
                    ? "This agent may have been archived or belongs to another workspace."
                    : "Give an agent a name, choose a role and start a conversation."}
                </p>
                <Button onClick={() => create()}>
                  <Plus size={17} />
                  Create agent
                </Button>
                <div className="role-preview">
                  {roles.map((r) => (
                    <button key={r.id} onClick={() => create(r.id)}>
                      <Avatar role={r.id} />
                      <strong>{r.name}</strong>
                      <small>{r.description}</small>
                    </button>
                  ))}
                </div>
              </div>
            ) : m.workspace.isLoading ? (
              <Spinner />
            ) : (
              <>
                {!messages.length && (
                  <div className="conversation-empty">
                    <Avatar role={agent.role} />
                    <h1>{agent.name}</h1>
                    <Badge>
                      {agent.role[0].toUpperCase() + agent.role.slice(1)}
                    </Badge>
                    <p>Your conversation starts here.</p>
                    <button
                      className="text-link"
                      onClick={() => setEditor("edit")}
                    >
                      Edit agent instructions <Settings2 size={14} />
                    </button>
                  </div>
                )}
                {messages.map((message) => (
                  <article
                    className={`message ${message.cards?.length ? "has-cards" : ""} ${message.author === "user" ? "user" : message.author === "system" ? "system" : ""}`}
                    key={message.id}
                  >
                    {message.author === "agent" && (
                      <Avatar role={agent.role} small />
                    )}
                    <div className="message-group">
                      <div className="bubble">
                        {message.author === "agent" ? (
                          <div className="markdown">
                            <ReactMarkdown
                              skipHtml
                              disallowedElements={["img"]}
                              components={{
                                a: (props) => (
                                  <a
                                    href={props.href}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                  >
                                    {props.children}
                                  </a>
                                ),
                              }}
                            >
                              {message.text.trim()}
                            </ReactMarkdown>
                          </div>
                        ) : (
                          message.text
                        )}
                      </div>
                      <span className="message-time">
                        {time(message.created_at)}
                      </span>
                      {message.cards?.map((card) => (
                        <div
                          className="message-card"
                          key={card.kind + card.target_id}
                        >
                          <ConversationCard
                            card={card}
                            m={m}
                            openConditions={() => openPanel("mandate", true)}
                            openPortfolio={() => openPanel("portfolio")}
                          />
                        </div>
                      ))}
                    </div>
                  </article>
                ))}
                {jobs.map((j) => (
                  <Spinner key={j.id} label={j.label} />
                ))}
                {w?.jobs
                  .slice(0, 1)
                  .filter((j) => j.status === "FAILED")
                  .map((j) => (
                    <Alert tone="error" key={j.id}>
                      {j.error || "The response could not be completed."}
                    </Alert>
                  ))}
              </>
            )}
          </div>
        </div>
        {!!w?.positions.length && !w.routines.some((r) => r.enabled) && (
          <div className="routine-invitation">
            <Repeat2 size={18} />
            <span>
              <strong>Keep an eye on your investment</strong>
              <small>
                Choose a daily check. Get an account alert when your conditions
                or the economics call for attention.
              </small>
            </span>
            <button onClick={() => openPanel("routines")}>Set routine</button>
          </div>
        )}
        {agent && (
          <div className="compose-area">
            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                send();
              }}
            >
              <label htmlFor="message" className="sr-only">
                Message {agent.name}
              </label>
              <textarea
                id="message"
                placeholder={`Message ${agent.name}…`}
                value={text}
                onChange={(e) => setDraft(e.target.value)}
                rows={2}
                maxLength={4000}
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing
                  ) {
                    e.preventDefault();
                    send();
                  }
                }}
              />
              {jobs.length ? (
                <button
                  type="button"
                  className="send-button"
                  aria-label="Stop response"
                  disabled={!!m.busy}
                  onClick={() => m.stop(jobs[0].id)}
                >
                  <Square size={15} fill="currentColor" />
                </button>
              ) : (
                <button
                  className="send-button"
                  type="submit"
                  aria-label="Send message"
                  disabled={!text.trim() || !!m.busy || !w}
                >
                  <ArrowUp size={19} />
                </button>
              )}
            </form>
            <div className="compose-meta">
              <div className="composer-tools">
                <button onClick={() => openPanel("mandate", true)}>
                  <SlidersHorizontal size={12} />
                  Conditions
                </button>
                <button
                  disabled={!!m.busy || !!jobs.length}
                  onClick={() =>
                    m.message(
                      agent.role,
                      "Compare two plans using my current conditions.",
                    )
                  }
                >
                  Compare two plans
                </button>
                <button onClick={() => openPanel("portfolio")}>
                  Portfolio
                </button>
              </div>
              <span role="status">
                {m.busy || "Shift + Enter for a new line"}
              </span>
            </div>
          </div>
        )}
      </main>
      {panel && (
        <SidePanel
          panel={panel}
          m={m}
          onClose={closePanel}
          theme={theme}
          setTheme={setTheme}
        />
      )}
      {editor && (
        <AgentEditor
          initialRole={initialRole}
          key={editor === "edit" ? agent?.id : "new"}
          agent={editor === "edit" ? agent : undefined}
          m={m}
          onClose={() => setEditor(null)}
          onSaved={(a) => {
            setEditor(null);
            choose(a.id);
          }}
          onArchived={() => {
            setEditor(null);
            nav(`/?network=${m.network}`);
          }}
        />
      )}
    </div>
  );
}
