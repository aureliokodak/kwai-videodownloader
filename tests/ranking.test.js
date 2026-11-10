const {test} = require('node:test');
const assert = require('node:assert/strict');
const {filterRanking} = require('../app/static/ranking.js');
const items = [
  {key:'a',title:'Novela #parte1',hashtags:['#novela','#parte1'],views:100,likes:0,comments:null,source:'feed-api',permalink:'https://www.kwai.com/video/1'},
  {key:'b',title:'Outro',hashtags:['#novelas'],views:200,likes:10,comments:5,source:'dom-card',media_url:'https://example.com/a.mp4'},
  {key:'c',title:'NOVELA',hashtags:['#novela'],views:300,likes:null,comments:0,source:'dom-card'}
];
test('combined inclusive ranges and exact hashtags',()=>assert.deepEqual(filterRanking(items,{views_min:100,views_max:200,hashtag:'#novela'}).map(x=>x.key),['a']));
test('requires every hashtag',()=>assert.deepEqual(filterRanking(items,{hashtag:'novela, parte1'}).map(x=>x.key),['a']));
test('unknown is different from zero',()=>assert.deepEqual(filterRanking(items,{likes_min:0,likes_max:0}).map(x=>x.key),['a']));
test('unknown metrics sort last in both directions',()=>{
  assert.deepEqual(filterRanking(items,{sort:'likes',direction:'asc'}).map(x=>x.key),['a','b','c']);
  assert.deepEqual(filterRanking(items,{sort:'likes'}).map(x=>x.key),['b','a','c']);
});
test('search source link and media combine',()=>assert.deepEqual(filterRanking(items,{search:'outro',source:'dom-card',link:'no',media:'yes'}).map(x=>x.key),['b']));
test('top applies after filtering and sorting without mutating collection',()=>{
  assert.deepEqual(filterRanking(items,{hashtag:'novela',top:1}).map(x=>x.key),['c']);
  assert.deepEqual(items.map(x=>x.key),['a','b','c']);
});
test('contradictory ranges are empty',()=>assert.equal(filterRanking(items,{views_min:300,views_max:100}).length,0));

const timed = items.map((item,i)=>({...item,duration_seconds:[30,60.5,null][i]}));
test('duration is optional and unknown durations remain visible without bounds',()=>{
  assert.deepEqual(filterRanking(timed,{duration_seconds_min:'',duration_seconds_max:''}).map(x=>x.key),['c','b','a']);
  assert.equal(filterRanking(items,{duration_seconds_min:0}).length,0);
});
test('duration bounds are inclusive, independent and combine with existing filters before top',()=>{
  assert.deepEqual(filterRanking(timed,{duration_seconds_min:30,duration_seconds_max:60.5}).map(x=>x.key),['b','a']);
  assert.deepEqual(filterRanking(timed,{duration_seconds_max:30}).map(x=>x.key),['a']);
  assert.deepEqual(filterRanking(timed,{duration_seconds_min:60}).map(x=>x.key),['b']);
  assert.deepEqual(filterRanking(timed,{duration_seconds_min:30,hashtag:'novela',top:1}).map(x=>x.key),['a']);
  assert.equal(filterRanking(timed,{duration_seconds_min:61,duration_seconds_max:30}).length,0);
});
