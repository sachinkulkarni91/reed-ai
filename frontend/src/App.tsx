import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import Meetings from "./pages/Meetings";
import MeetingDetail from "./pages/MeetingDetail";
import ActionItems from "./pages/ActionItems";
import Settings from "./pages/Settings";
import Ask from "./pages/Ask";
import Speakers from "./pages/Speakers";

const NAV = [
  { to: "/meetings", label: "Meetings", icon: "▤" },
  { to: "/action-items", label: "Action items", icon: "✓" },
  { to: "/speakers", label: "Speakers", icon: "♪" },
  { to: "/ask", label: "Ask across meetings", icon: "✦" },
  { to: "/settings", label: "Settings", icon: "⚙" },
];

export default function App() {
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-60 shrink-0 border-r border-ink-800 bg-ink-900/50 p-5 md:block">
        <div className="mb-8 flex items-center gap-2">
          <div className="grid h-8 w-8 place-items-center rounded-lg bg-reed-600 font-bold text-white">
            R
          </div>
          <div>
            <div className="text-sm font-semibold text-white">Reed AI</div>
            <div className="text-[11px] text-slate-500">Meeting intelligence</div>
          </div>
        </div>
        <nav className="space-y-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
                  isActive
                    ? "bg-ink-800 font-medium text-white"
                    : "text-slate-400 hover:bg-ink-800/60 hover:text-slate-200"
                }`
              }
            >
              <span className="w-4 text-center text-slate-500">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <main className="min-w-0 flex-1">
        <Routes>
          <Route path="/" element={<Navigate to="/meetings" replace />} />
          <Route path="/meetings" element={<Meetings />} />
          <Route path="/meetings/:id" element={<MeetingDetail />} />
          <Route path="/action-items" element={<ActionItems />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/speakers" element={<Speakers />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
