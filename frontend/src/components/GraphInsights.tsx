import { useState, useEffect } from 'react'
import Icon from './ui/Icon'

interface Insight {
  type: 'warning' | 'reminder' | 'info'
  priority: 'high' | 'medium' | 'low'
  message: string
}

interface GraphInsights {
  forgotten_characters: { entity_id: string; name: string; last_seen_time_order: number; important: boolean }[]
  unresolved_foreshadows: { id: string; text: string; related_entities: string[] }[]
  disconnected_pairs: { entity_a: { id: string; name: string }; entity_b: { id: string; name: string }; warning: string }[]
  bridge_characters: { entity_id: string; entity_name: string; bridge_count: number; would_disconnect: string[][] }[]
  underutilized_locations: string[]
  suggestions: Insight[]
  confidence_scores?: { entity_id: string; entity_name: string; entity_type: string; confidence: number; stars: number; recommendation: string }[]
  constraint_violations?: { constraint_id: string; description: string; severity: string; violations: Record<string, string>[] }[]
}

const PRIORITY_COLORS = {
  high: 'text-red-400 bg-red-600/20 border-red-600',
  medium: 'text-yellow-400 bg-yellow-600/20 border-yellow-600',
  low: 'text-blue-400 bg-blue-600/20 border-blue-600',
}

const TYPE_ICONS = {
  warning: 'alert-triangle',
  reminder: 'bell',
  info: 'info',
}

export default function GraphInsights({ bookId }: { bookId: string }) {
  const [insights, setInsights] = useState<GraphInsights | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => { loadInsights() }, [bookId])

  async function loadInsights() {
    try {
      const res = await fetch(`/api/books/${bookId}/graph/insights`)
      if (res.ok) {
        const data = await res.json()
        // Ensure all fields exist with defaults
        setInsights({
          forgotten_characters: data.forgotten_characters || [],
          unresolved_foreshadows: data.unresolved_foreshadows || [],
          disconnected_pairs: data.disconnected_pairs || [],
          bridge_characters: data.bridge_characters || [],
          underutilized_locations: data.underutilized_locations || [],
          suggestions: data.suggestions || [],
          confidence_scores: data.confidence_scores || [],
          constraint_violations: data.constraint_violations || [],
        })
      }
    } catch (e) {
      console.error('Insights fetch failed:', e)
    }
    setLoading(false)
  }

  if (loading) return <div className="flex-1 flex items-center justify-center text-zinc-600 text-sm">分析图谱中...</div>
  if (!insights) return <div className="flex-1 flex items-center justify-center text-zinc-600 text-sm">无法加载洞察数据</div>

  const hasInsights = (insights.suggestions?.length || 0) > 0 || 
                      (insights.forgotten_characters?.length || 0) > 0 || 
                      (insights.unresolved_foreshadows?.length || 0) > 0 ||
                      (insights.confidence_scores?.length || 0) > 0 ||
                      (insights.constraint_violations?.length || 0) > 0

  if (!hasInsights) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center text-zinc-600 p-8">
        <Icon name="check-circle" size={48} className="text-emerald-600 mb-4" />
        <h3 className="text-sm font-medium text-zinc-400 mb-1">世界观状态良好</h3>
        <p className="text-xs text-zinc-600 text-center">没有发现需要特别关注的叙事问题</p>
      </div>
    )
  }

  return (
    <div className="flex-1 w-full overflow-y-auto p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
          <Icon name="brain" size={16} /> 图谱洞察
        </h2>
        <button 
          onClick={loadInsights}
          className="text-xs text-zinc-500 hover:text-zinc-300 flex items-center gap-1"
        >
          <Icon name="refresh-cw" size={12} /> 刷新
        </button>
      </div>

      {/* Suggestions */}
      {(insights.suggestions?.length || 0) > 0 && (
        <div className="space-y-2">
          <h3 className="text-xs font-medium text-zinc-400 flex items-center gap-1.5">
            <Icon name="lightbulb" size={14} /> 写作建议
          </h3>
          {insights.suggestions.map((s, i) => (
            <div key={i} className={`p-3 rounded-lg border ${PRIORITY_COLORS[s.priority]}`}>
              <div className="flex items-start gap-2">
                <Icon name={TYPE_ICONS[s.type]} size={14} className="mt-0.5 shrink-0" />
                <p className="text-xs leading-relaxed">{s.message}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Forgotten characters */}
      {(insights.forgotten_characters?.length || 0) > 0 && (
        <div className="bg-zinc-900/50 border border-red-800/50 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Icon name="user-x" size={14} className="text-red-500" />
            <span className="text-xs text-red-400 font-medium">被遗忘的重要角色</span>
          </div>
          <div className="space-y-1.5">
            {insights.forgotten_characters.map(c => (
              <div key={c.entity_id} className="flex items-center justify-between text-[10px]">
                <span className="text-zinc-300">{c.name}</span>
                <span className="text-zinc-600">
                  最后出现: T{c.last_seen_time_order >= 0 ? c.last_seen_time_order : '从未'}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Unresolved foreshadows */}
      {(insights.unresolved_foreshadows?.length || 0) > 0 && (
        <div className="bg-zinc-900/50 border border-yellow-800/50 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Icon name="target" size={14} className="text-yellow-500" />
            <span className="text-xs text-yellow-400 font-medium">
              待回收伏笔 ({insights.unresolved_foreshadows.length})
            </span>
          </div>
          <div className="space-y-1.5 max-h-40 overflow-y-auto">
            {insights.unresolved_foreshadows.map(f => (
              <div key={f.id} className="text-[10px]">
                <p className="text-zinc-400 truncate">{f.text}</p>
                {f.related_entities && f.related_entities.length > 0 && (
                  <p className="text-zinc-600 mt-0.5">
                    关联: {f.related_entities.slice(0, 3).join(', ')}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Bridge characters */}
      {(insights.bridge_characters?.length || 0) > 0 && (
        <div className="bg-zinc-900/50 border border-blue-800/50 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Icon name="git-merge" size={14} className="text-blue-500" />
            <span className="text-xs text-blue-400 font-medium">关键枢纽角色</span>
          </div>
          <div className="space-y-1.5">
            {insights.bridge_characters.map(b => (
              <div key={b.entity_id} className="flex items-center justify-between text-[10px]">
                <span className="text-zinc-300">{b.entity_name}</span>
                <span className="text-zinc-600">
                  连接 {b.bridge_count} 对角色
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Disconnected pairs */}
      {(insights.disconnected_pairs?.length || 0) > 0 && (
        <div className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Icon name="unlink" size={14} className="text-zinc-500" />
            <span className="text-xs text-zinc-400 font-medium">无关联角色对</span>
          </div>
          <div className="space-y-1.5 max-h-32 overflow-y-auto">
            {insights.disconnected_pairs.slice(0, 5).map((p, i) => (
              <div key={i} className="flex items-center gap-2 text-[10px] text-zinc-500">
                <span className="text-zinc-400">{p.entity_a.name}</span>
                <span className="text-zinc-700">↔</span>
                <span className="text-zinc-400">{p.entity_b.name}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Underutilized locations */}
      {(insights.underutilized_locations?.length || 0) > 0 && (
        <div className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Icon name="map-pin" size={14} className="text-zinc-500" />
            <span className="text-xs text-zinc-400 font-medium">未使用地点</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {insights.underutilized_locations.map((loc, i) => (
              <span key={i} className="text-[10px] px-2 py-0.5 bg-zinc-800 text-zinc-500 rounded">
                {loc}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Confidence scores */}
      {(insights.confidence_scores?.length || 0) > 0 && (
        <div className="bg-zinc-900/50 border border-violet-800/50 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Icon name="activity" size={14} className="text-violet-500" />
            <span className="text-xs text-violet-400 font-medium">设定可信度</span>
          </div>
          <div className="space-y-1.5 max-h-48 overflow-y-auto">
            {insights.confidence_scores.slice(0, 10).map(s => {
              const stars = '★'.repeat(s.stars) + '☆'.repeat(5 - s.stars)
              const isLow = s.confidence < 0.3
              return (
                <div key={s.entity_id} className="flex items-center justify-between text-[10px]">
                  <span className={`${isLow ? 'text-red-400' : 'text-zinc-300'}`}>
                    {s.entity_name}
                    <span className="text-zinc-600 ml-1">({s.confidence.toFixed(2)})</span>
                  </span>
                  <span className={isLow ? 'text-red-500' : 'text-violet-500'}>{stars}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Constraint violations */}
      {(insights.constraint_violations?.length || 0) > 0 && (
        <div className="bg-zinc-900/50 border border-red-800/50 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Icon name="shield" size={14} className="text-red-500" />
            <span className="text-xs text-red-400 font-medium">
              约束违反 ({insights.constraint_violations.length})
            </span>
          </div>
          <div className="space-y-2 max-h-48 overflow-y-auto">
            {insights.constraint_violations.map(v => {
              const sevColor = v.severity === 'hard' ? 'text-red-400' : 'text-yellow-400'
              return (
                <div key={v.constraint_id} className="text-[10px]">
                  <p className={`${sevColor} truncate`}>{v.description}</p>
                  {v.violations && v.violations.length > 0 && (
                    <p className="text-zinc-600 mt-0.5">
                      违反 {v.violations.length} 处
                    </p>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
