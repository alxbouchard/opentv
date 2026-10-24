// lance le service OpenTV du .wgt avec un faux tizen (Node)
global.tizen = { application: {
  getAppsInfo: function (ok) { ok(require('./apps.json')); },
  launch: function (id, ok) { console.log('SVC_LAUNCH', id); ok && ok(); }
}};
require('./wgt/service/index.js');
