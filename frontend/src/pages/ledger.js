import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {ingestResultLabels, ledgerModeLabels, ledgerStateLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const MODULE = 4;

function migrationBanner(mig) {
  if (!mig) return '';
  return panel(`${esc(mig.label)} 拆分进行中`, `
    <dl class="metrics">
      <div><dt>状态</dt><dd>${esc(ledgerStateLabels[mig.state] || mig.state)}</dd></div>
      <div><dt>已复制</dt><dd>${esc(mig.copied)}</dd></div>
      <div><dt>月表张数</dt><dd>${esc(mig.monthly_count)}</dd></div>
      <div><dt>日分区张数</dt><dd>${esc(mig.daily_count)}</dd></div>
    </dl>
    <p class="note">拆分期间闸机补传双写入账，查询仍走月表 <code>${esc(mig.monthly_table)}</code>；对齐后整月一次性切换到日分区。</p>
    ${mig.last_error ? `<p class="error">${esc(mig.last_error)}</p>` : ''}`);
}

function queryResults(query) {
  if (!query) return '';
  const rows = query.rows.map(row=>[esc(row.slip_id), esc(row.entered_on), esc(row.source), esc(date(row.received_at)), `<code>${esc(row.partition)}</code>`]);
  const partitions = [...new Set(query.rows.map(row=>row.partition))];
  return panel(`查账结果（${esc(query.from)} 至 ${esc(query.to)}）`,
    (rows.length ? records(['凭条编号', '入园日期', '来源', '回执时间', '所在分区'], rows) : '<p class="empty">该时段没有凭条。</p>')
    + (query.truncated ? `<p class="note">结果过多，仅显示前 ${rows.length} 张，请缩小日期范围。</p>` : '')
    + `<p class="note">涉及分区：${partitions.length ? partitions.map(name=>`<code>${esc(name)}</code>`).join(' ') : '—'}</p>`);
}

function ingestAcks(acks) {
  if (!acks) return '';
  const rows = acks.map(ack=>[esc(ack.slip_id), esc(ack.entered_on), esc(ingestResultLabels[ack.result] || ack.result), `<code>${esc(ack.partition || '—')}</code>`]);
  return panel('补传回执', records(['凭条编号', '入园日期', '结果', '所在分区'], rows));
}

export function ledgerPage(state) {
  const {ledger, session} = state;
  if (!ledger) return heading(topic.modules[MODULE], '账簿加载中……');
  const operator = session.role==='operator';
  const query = state.ledgerQuery;
  const today = new Date().toLocaleDateString('sv-SE');
  const monthRows = ledger.months.map(row=>[
    esc(row.label),
    esc(ledgerModeLabels[row.mode] || row.mode),
    `<code>${esc(row.table)}</code>`,
    esc(row.count),
    operator && row.mode==='monthly' && !ledger.migration ? `<button class="secondary" data-split="${esc(row.month)}">拆分为日分区</button>` : '',
  ]);
  monthRows.push([esc('待核池'), '', `<code>${esc(ledger.default_table)}</code>`, esc(ledger.default_count), '']);
  const history = ledger.migrations.map(row=>[
    esc(row.label),
    esc(ledgerStateLabels[row.state] || row.state),
    esc(row.copied),
    esc(date(row.switched_at)),
    row.last_error ? `<span class="error">${esc(row.last_error)}</span>` : '',
  ]);
  return `${heading(topic.modules[MODULE], '闸机补传入账、按日期查账，节庆月在线拆分为日分区。')}
    ${migrationBanner(ledger.migration)}
    ${panel('月分区总览', records(['月份', '模式', '物理表', '张数', '操作'], monthRows)
      + (operator ? `<form id="month-form" class="inline-form"><label>登记新月份<input name="month" placeholder="YYYY-MM-01" pattern="\\d{4}-\\d{2}-01" maxlength="10" required></label><button class="secondary">登记月份</button></form>` : ''))}
    ${panel('按日期查账', `<form id="slip-query-form" class="inline-form">
      <label>起始日期<input type="date" name="date_from" value="${esc(query?.from || today)}" required></label>
      <label>结束日期<input type="date" name="date_to" value="${esc(query?.to || today)}" required></label>
      <button>查询</button></form>`)}
    ${queryResults(query)}
    ${operator ? panel('闸机补传入账', `<form id="ingest-form">
      <label class="field"><span>补传清单（JSON 数组，字段 slip_id、entered_on，source 可选）</span>
      <textarea name="records" rows="5" required placeholder='[{"slip_id":"SM-1122-01","entered_on":"${today}","source":"闸机补传"}]'></textarea></label>
      <div class="actions form-actions"><button>入账并回执</button></div></form>`) : ''}
    ${ingestAcks(state.ledgerAcks)}
    ${history.length ? panel('拆分记录', records(['月份', '状态', '已复制张数', '切换时间', '备注'], history)) : ''}`;
}

export function bindLedger(state, api, refresh, fail) {
  submit('slip-query-form', async data=>{
    const from = data.get('date_from');
    const to = data.get('date_to');
    const result = await api(`/ledger/slips?date_from=${encodeURIComponent(from)}&date_to=${encodeURIComponent(to)}`);
    state.ledgerQuery = {...result, from, to};
    await refresh();
  }, fail);
  submit('ingest-form', async data=>{
    let entries;
    try { entries = JSON.parse(data.get('records')); }
    catch { throw new Error('补传清单须为 JSON 数组'); }
    const result = await api('/ledger/ingest', {method:'POST', data:{records:entries}});
    state.ledgerAcks = result.acks;
    await refresh();
  }, fail);
  submit('month-form', async data=>{
    await api('/ledger/months', {method:'POST', data:{month:data.get('month')}});
    await refresh();
  }, fail);
  document.querySelectorAll('[data-split]').forEach(button=>button.addEventListener('click', async()=>{
    try {
      await api('/ledger/migrations', {method:'POST', data:{month:button.dataset.split}});
      await refresh();
    } catch (error) { fail(error); }
  }));
}
