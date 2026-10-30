/**
 * ECharts 按需注册（ECharts 5.6 支持 echarts/core + echarts/charts 模块化引入）。
 * 只注册本项目实际用到的图表与组件，bundle 体积约为全量引入的 40%。
 * 仅在此处调用 use()，全局一次；具体页面通过 components/ui/Chart.tsx 使用。
 */
import * as echarts from 'echarts/core'
import { BarChart, LineChart, ScatterChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  ScatterChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
])

export { echarts }
