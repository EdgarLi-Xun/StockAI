<script setup lang="ts">
// 早报里的想法评审：总表、留下的卡片、今日对照和一句不该做的事。
defineProps<{ review: any }>()
</script>

<template>
  <section class="idea-review">
    <h3>想法评审</h3>
    <p>{{ review.statement }}</p>
    <p v-if="review.waiting">想法池未填写，本日不评审。</p>
    <template v-else>
      <h3>总表</h3>
      <div class="table-scroll">
        <table class="watch-table">
          <thead><tr><th>想法</th><th>过关</th><th>结论</th></tr></thead>
          <tbody><tr v-for="(row, index) in review.summary" :key="index"><td>{{ row.sentence }}</td><td>{{ row.gates }}</td><td>{{ row.conclusion }}</td></tr></tbody>
        </table>
      </div>
      <h3>留下的想法</h3>
      <p v-if="!review.survivors.length">这次没有留下的想法。</p>
      <article v-for="card in review.survivors" :key="card.idea_id" class="report-candidate">
        <h3>{{ card.sentence }}</h3>
        <p>过了 {{ card.gates_passed }} 关。留下的原因：{{ card.reason }}</p>
        <p>最大风险：{{ card.max_risk }}</p>
        <p>失效条件：{{ card.invalidation }}</p>
        <p v-for="(source, index) in card.sources" :key="index">来源：{{ source.source }} · {{ source.fetched_at }}</p>
      </article>
      <h3>其余死因</h3>
      <p v-for="item in review.deaths" :key="item.idea_id">{{ item.sentence }}：{{ item.reason }}</p>
    </template>
    <h3>今日对照</h3>
    <p v-if="review.comparison.status !== '已对照'">{{ review.comparison.detail }}</p>
    <template v-else>
      <p>{{ review.comparison.change_note }}。来源：{{ review.comparison.source }}，采集时间：{{ review.comparison.fetched_at }}。</p>
      <p>利好：{{ review.comparison.helps.join('；') }}</p>
      <p>不利：{{ review.comparison.hurts.join('；') }}</p>
    </template>
    <h3>今天最不该做的事</h3>
    <p>{{ review.avoid }}</p>
    <template v-if="review.weekly">
      <h3>本周死因</h3>
      <p>{{ review.weekly.most }}</p>
      <p v-for="item in review.weekly.ranked" :key="item.reason">{{ item.reason }}：{{ item.count }}次</p>
    </template>
    <p>{{ review.disclaimer }}。{{ review.estimate_note }}。</p>
  </section>
</template>
