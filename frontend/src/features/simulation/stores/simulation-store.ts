// 推演功能3.0 — 模拟会话本地状态管理

import { createStore } from '../../../store'
import type { SessionInfo, TurnEvent, SimChoice } from '../types'

interface SimStoreState {
  sessions: SessionInfo[]
  currentSimId: string | null
  turns: TurnEvent[]
  branches: any[]
  currentBranchId: string | null
  loading: boolean
  error: string | null
  streaming: boolean
  narrative: string
  choices: SimChoice[]
  choicePrompt: string
  hotChoices: string[]
  statusText: string
  lastEvent: string | null
}

const initialState: SimStoreState = {
  sessions: [],
  currentSimId: null,
  turns: [],
  branches: [],
  currentBranchId: null,
  loading: false,
  error: null,
  streaming: false,
  narrative: '',
  choices: [],
  choicePrompt: '',
  hotChoices: [],
  statusText: '',
  lastEvent: null,
}

export const simStore = createStore<SimStoreState>(initialState)

// ── Setters ──

export function setSimSessions(sessions: SessionInfo[]) {
  simStore.setState({ sessions })
}

export function setCurrentSimId(id: string | null) {
  simStore.setState({ currentSimId: id })
}

export function setSimTurns(turns: TurnEvent[]) {
  simStore.setState({ turns })
}

export function setSimBranches(branches: any[]) {
  simStore.setState({ branches })
}

export function setSimLoading(loading: boolean) {
  simStore.setState({ loading })
}

export function setSimError(error: string | null) {
  simStore.setState({ error })
}

export function initSimRun(
  sessionId: string = '',
  turns: TurnEvent[] = [],
  choices: SimChoice[] = [],
  hotChoices: string[] = [],
  narrative: string = '',
) {
  simStore.setState({
    currentSimId: sessionId || null,
    turns,
    choices,
    choicePrompt: '',
    hotChoices,
    narrative,
    streaming: false,
    error: null,
    lastEvent: null,
  })
}

export function setSimStreaming(streaming: boolean) {
  simStore.setState({ streaming })
}

export function appendSimNarrative(chunk: string) {
  const current = simStore.getState().narrative
  simStore.setState({ narrative: current + chunk })
}

export function setSimNarrative(narrative: string) {
  simStore.setState({ narrative })
}

export function setSimChoices(choices: SimChoice[], choicePrompt: string = '') {
  simStore.setState({ choices, choicePrompt })
}

export function setSimHotChoices(hotChoices: string[]) {
  simStore.setState({ hotChoices })
}

export function setSimStatusText(text: string) {
  simStore.setState({ statusText: text })
}

export function setSimLastEvent(event: string | null) {
  simStore.setState({ lastEvent: event })
}

export function resetSimRun() {
  simStore.setState({
    currentSimId: null,
    turns: [],
    branches: [],
    currentBranchId: null,
    loading: false,
    error: null,
    streaming: false,
    narrative: '',
    choices: [],
    choicePrompt: '',
    hotChoices: [],
    statusText: '',
    lastEvent: null,
  })
}

// ── Selectors (React hooks) ──

export function useSimSessions(): SessionInfo[] {
  return simStore.useStore(s => s.sessions)
}

export function useCurrentSimId(): string | null {
  return simStore.useStore(s => s.currentSimId)
}

export function useSimTurns(): TurnEvent[] {
  return simStore.useStore(s => s.turns)
}

export function useSimBranches(): any[] {
  return simStore.useStore(s => s.branches)
}

export function useSimLoading(): boolean {
  return simStore.useStore(s => s.loading)
}

export function useSimError(): string | null {
  return simStore.useStore(s => s.error)
}

export function useSimRunState(): {
  narrative: string
  choices: SimChoice[]
  choicePrompt: string
  hotChoices: string[]
  streaming: boolean
  statusText: string
  lastEvent: string | null
  loading: boolean
  error: string | null
} {
  return simStore.useStore(s => ({
    narrative: s.narrative,
    choices: s.choices,
    choicePrompt: s.choicePrompt,
    hotChoices: s.hotChoices,
    streaming: s.streaming,
    statusText: s.statusText,
    lastEvent: s.lastEvent,
    loading: s.loading,
    error: s.error,
  }))
}
