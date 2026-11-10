/* Pure ranking rules shared by rendering, selection and exports. */
(function (root) {
  function filterRanking(items, filters = {}) {
    const query = String(filters.search || '').trim().toLocaleLowerCase('pt-BR');
    const tags = String(filters.hashtag || '').toLowerCase().split(/[\s,]+/).filter(Boolean).map(x => '#' + x.replace(/^#+/, ''));
    const result = items.filter(item => {
      if (query && ![item.title, item.photo_id, item.key, ...(item.hashtags || [])].join(' ').toLocaleLowerCase('pt-BR').includes(query)) return false;
      const actualTags = (item.hashtags || []).map(x => x.toLowerCase());
      if (!tags.every(tag => actualTags.includes(tag))) return false;
      for (const metric of ['views', 'likes', 'comments', 'duration_seconds']) {
        for (const bound of ['min', 'max']) {
          const value = filters[metric + '_' + bound];
          if (value === '' || value == null) continue;
          if (item[metric] == null || (bound === 'min' ? item[metric] < Number(value) : item[metric] > Number(value))) return false;
        }
      }
      if (filters.link === 'yes' && !item.permalink || filters.link === 'no' && item.permalink) return false;
      if (filters.media === 'yes' && !item.media_url || filters.media === 'no' && item.media_url) return false;
      return !filters.source || item.source === filters.source;
    });
    const metric = ['views', 'likes', 'comments'].includes(filters.sort) ? filters.sort : 'views';
    const direction = filters.direction === 'asc' ? 1 : -1;
    result.sort((a, b) => {
      if (a[metric] == null && b[metric] != null) return 1;
      if (b[metric] == null && a[metric] != null) return -1;
      return ((a[metric] ?? 0) - (b[metric] ?? 0)) * direction || String(a.key).localeCompare(String(b.key));
    });
    return Number(filters.top) > 0 ? result.slice(0, Number(filters.top)) : result;
  }
  root.filterRanking = filterRanking;
  if (typeof module !== 'undefined') module.exports = { filterRanking };
})(globalThis);
