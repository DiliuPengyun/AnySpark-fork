import { useState, useEffect, useMemo } from 'react'
import Icon from './ui/Icon'

interface MetricsData {
  entity_count: number
  relation_count: number
  density: number
  isolated_entities: { name: string; type: string }[]
  isolated_count: number
  largest_component_size: number
  health_assessment: string
}

const HEALTH_COLORS: Record<string, string> = {
  '良好': 'text-emerald-400',
  '一般': 'text-yellow-400',
  '稀疏': 'text-red-400',
}

const HEALTH_BG: Record<string, string> = {
  '良好': 'bg-emerald-600/20 border-emerald-600',
  '一般': 'bg-yellow-600/20 border-yellow-600',
  '稀疏': 'bg-red-600/20 border-red-600',
}

export default function WorldbuildingMetrics({ bookId }: { bookId: string }) {
  const [metrics, setMetrics] = useState<MetricsData | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => { loadMetrics() }, [bookId])

  async function loadMetrics() {
    try {
      const res = await fetch(`/api/books/${bookId}/graph/metrics`)
      if (res.ok) setMetrics(await res.json())
    } catch (e) {
      console.error('Metrics fetch failed:', e)
    }
    setLoading(false)
  }

  const densityPercent = useMemo(() => {
    if (!metrics) return 0
    return Math.round(metrics.density * 1000) / 10
  }, [metrics])

  const connectivityPercent = useMemo(() => {
    if (!metrics || metrics.entity_count === 0) return 0
    return Math.round((metrics.largest_component_size / metrics.entity_count) * 100)
  }, [metrics])

  if (loading) return <div className="flex-1 flex items-center justify-center text-zinc-600 text-sm">加载指标中...</div>
  if (!metrics) return <div className="flex-1 flex items-center justify-center text-zinc-600 text-sm">无法加载指标数据</div>

  return (
    <div className="flex-1 w-full overflow-y-auto p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-zinc-200 flex items-center gap-2">
          <Icon name="activity" size={16} /> 世界观健康度
        </h2>
        <span className={`px-2 py-1 rounded border text-xs font-medium ${HEALTH_BG[metrics.health_assessment] || HEALTH_BG['稀疏']} ${HEALTH_COLORS[metrics.health_assessment] || HEALTH_COLORS['稀疏']}`}>
          {metrics.health_assessment}
        </span>
      </div>

      {/* Key metrics grid */}
      <div className="grid grid-cols-2 gap-3">
        {/* Entity count */}
        <div className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-1">
            <Icon name="users" size={14} className="text-zinc-500" />
            <span className="text-[10px] text-zinc-500">实体总数</span>
          </div>
          <div className="text-2xl font-bold text-zinc-200">{metrics.entity_count}</div>
        </div>

        {/* Relation count */}
        <div className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-1">
            <Icon name="link" size={14} className="text-zinc-500" />
            <span className="text-[10px] text-zinc-500">关系总数</span>
          </div>
          <div className="text-2xl font-bold text-zinc-200">{metrics.relation_count}</div>
        </div>

        {/* Density */}
        <div className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-1">
            <Icon name="bar-chart" size={14} className="text-zinc-500" />
            <span className="text-[10px] text-zinc-500">连接密度</span>
          </div>
          <div className="text-2xl font-bold text-zinc-200">{densityPercent}%</div>
          <div className="mt-1 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
            <div 
              className="h-full bg-blue-500 rounded-full transition-all"
              style={{ width: `${Math.min(densityPercent * 5, 100)}%` }}
            />
          </div>
        </div>

        {/* Connectivity */}
        <div className="bg-zinc-900/50 border border-zinc-800 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-1">
            <Icon name="git-merge" size={14} className="text-zinc-500" />
            <span className="text-[10px] text-zinc-500">主连通率</span>
          </div>
          <div className="text-2xl font-bold text-zinc-200">{connectivityPercent}%</div>
          <div className="mt-1 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
            <div 
              className="h-full bg-emerald-500 rounded-full transition-all"
              style={{ width: `${connectivityPercent}%` }}
            />
          </div>
        </div>
      </div>

      {/* Isolated entities */}
      {metrics.isolated_count > 0 && (
        <div className="bg-zinc-900/50 border border-yellow-800/50 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Icon name="alert-triangle" size={14} className="text-yellow-500" />
            <span className="text-xs text-yellow-400 font-medium">孤立实体 ({metrics.isolated_count})</span>
          </div>
          <div className="space-y-1 max-h-32 overflow-y-auto">
            {metrics.isolated_entities.slice(0, 10).map((e, i) => (
              <div key={i} className="flex items-center gap-2 text-[10px] text-zinc-400">
                <span className="w-1.5 h-1.5 rounded-full bg-yellow-600" />
                <span>{e.name}</span>
                <span className="text-zinc-600">({e.type})</span>
              </div>
            ))}
            {metrics.isolated_count > 10 && (
              <div className="text-[10px] text-zinc-600 pt-1">...还有 {metrics.isolated_count - 10} 个孤立实体</div>
            )}
          </div>
        </div>
      )}

      {/* Tips */}
      <div className="bg-zinc-900/30 border border-zinc-800 rounded-lg p-3">
        <div className="flex items-center gap-2 mb-2">
          <Icon name="lightbulb" size={14} className="text-blue-500" />
          <span className="text-xs text-blue-400 font-medium">写作建议</span>
        </div>
        <ul className="space-y-1.5 text-[10px] text-zinc-400">
          {metrics.density < 0.05 && (
            <li className="flex items-start gap-1.5">
              <span className="text-yellow-500 mt-0.5">•</span>
              <span>连接密度较低，可以考虑添加更多角色之间的关系</span>
            </li>
          )}
          {metrics.isolated_count > 0 && (
            <li className="flex items-start gap-1.5">
              <span className="text-yellow-500 mt-0.5">•</span>
              <span>有 {metrics.isolated_count} 个孤立实体，建议将它们融入主线剧情</span>
            </li>
          )}
          {connectivityPercent < 80 && (
            <li className="flex items-start gap-1.5">
              <span className="text-yellow-500 mt-0.5">•</span>
              <span>主连通率偏低，可能存在多个独立的故事线</span>
            </li>
          )}
          {metrics.density >= 0.15 && metrics.isolated_count === 0 && (
            <li className="flex items-start gap-1.5">
              <span className="text-emerald-500 mt-0.5">✓</span>
              <span>世界观结构良好，实体之间连接紧密</span>
            </li>
          )}
        </ul>
      </div>
    </div>
  )
}
