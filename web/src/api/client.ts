import axios, { AxiosError } from 'axios'

export interface ApiProblem {
  code: string
  message: string
  trace_id: string
  details: Array<Record<string, unknown>>
}

const client = axios.create({
  baseURL: '/api/v1',
  timeout: 8000,
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.response.use(
  (r) => r,
  (error: AxiosError<ApiProblem>) => {
    const problem = error.response?.data
    const message = problem?.message ?? error.message ?? '网络异常'
    return Promise.reject(new Error(`[${problem?.code ?? 'NETWORK'}] ${message}`))
  },
)

export async function getHistory(
  pointId: number,
  from: string,
  to: string,
  interval = '1m',
): Promise<Array<{ ts: string; value: number | null }>> {
  const { data } = await client.get(`/points/${pointId}/history`, {
    params: { from, to, interval },
  })
  return data.points
}

export async function getLatest(pointId: number): Promise<{ ts: string; value: number | null }> {
  const { data } = await client.get(`/points/${pointId}/latest`)
  return data
}
