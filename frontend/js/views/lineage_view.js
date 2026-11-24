export const lineageViewTemplate = `
<section class="panel">
  <h2>轴重血缘</h2>
  <p class="hint">记录驳载分票、二次过磅等轴重血缘边。</p>
  <div class="form-row">
    <div><label>起点票</label>
      <select v-model.number="lineageForm.from_ticket_id">
        <option v-for="t in tickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>终点票(可空)</label>
      <select v-model.number="lineageForm.to_ticket_id">
        <option :value="null">—</option>
        <option v-for="t in tickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>类型</label><input v-model="lineageForm.edge_type"/></div>
    <div><label>说明</label><input v-model="lineageForm.detail"/></div>
  </div>
  <div class="actions">
    <button @click="addLineage">登记边</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <table class="data-table">
    <thead><tr><th>自</th><th>至</th><th>类型</th><th>说明</th><th>闸代</th></tr></thead>
    <tbody>
      <tr v-for="e in lineageEdges" :key="e.id">
        <td>{{ e.from_code || e.from_ticket_id }}</td>
        <td>{{ e.to_code || '—' }}</td>
        <td>{{ e.edge_type }}</td>
        <td>{{ e.detail }}</td>
        <td>
          <span v-if="e.from_seal_generation" class="badge warn">闸代 {{ e.from_seal_generation }} 生效</span>
          <span v-else class="muted">—</span>
        </td>
      </tr>
    </tbody>
  </table>
  <p class="hint">封后禁改：闸代生效后不得登记轴重修订边，影子草案不得混入血缘。</p>
</section>
`;