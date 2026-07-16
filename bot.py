# -*- coding: utf-8 -*-
"""VIP Taxi Bot: AI, tariffs, special requests, driver registration photos."""
import asyncio, html, json, logging, os, re, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, PicklePersistence, filters

BOT_TOKEN=os.getenv('BOT_TOKEN','')
OPENROUTER_API_KEY=os.getenv('OPENROUTER_API_KEY','')
OPENROUTER_MODEL=os.getenv('OPENROUTER_MODEL','openrouter/free')
MODERATION_CHAT_ID=int(os.getenv('MODERATION_CHAT_ID','-5062249297'))
ORDERS_CHAT_ID=int(os.getenv('ORDERS_CHAT_ID','-1003446115764'))
PERSISTENCE_PATH=os.getenv('PERSISTENCE_PATH','bot_state.pickle')
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',level=logging.INFO)
logger=logging.getLogger(__name__)

(ORDER_NAME,ORDER_FROM,ORDER_TO,ORDER_TIME,ORDER_CLASS,ORDER_TARIFF,ORDER_HOURS,ORDER_COMMENT,ORDER_SPECIAL,ORDER_CONFIRM)=range(10)
(REG_NAME,REG_PHONE,REG_CAR,REG_YEAR,REG_PLATE,REG_CLASS,REG_DOCS,REG_CAR_PHOTOS,REG_CONFIRM)=range(20,29)
CAR_CLASSES={'Business','First','Lux','Минивэн'}
ORDER_CLASSES=CAR_CLASSES|{'Неважно'}
HOURLY_RATES={'Business':2500,'First':5000,'Lux':7000,'Минивэн':4000}
AIRPORT_RATES={
 'svo_vko':{'Business':5000,'First':10000,'Lux':12000,'Минивэн':10000},
 'dme_zia':{'Business':7000,'First':12000,'Lux':14000,'Минивэн':12000},
}
AI_WELCOME=('🤖 VIP Taxi AI\n\nОпишите поездку одним сообщением — я заполню заказ и уточню недостающие данные.\n\n'
            'Например: завтра в 10:00 из Шереметьево в Москва-Сити, First, 2 пассажира.')
MAIN_KB=ReplyKeyboardMarkup([['🚖 Заказать поездку','✨ Особый запрос'],['👨‍✈️ Стать водителем','📋 Мой статус']],resize_keyboard=True)
LOCATION_KB=ReplyKeyboardMarkup([[KeyboardButton('📍 Отправить геопозицию',request_location=True)],['✍️ Ввести адрес']],resize_keyboard=True)
TIME_KB=ReplyKeyboardMarkup([['Сейчас'],['Указать дату и время']],resize_keyboard=True)
TARIFF_KB=ReplyKeyboardMarkup([['Разовая поездка'],['Почасовая'],['Аэропорт'],['Бизнес-день']],resize_keyboard=True)
CLASS_KB=ReplyKeyboardMarkup([['Business','First'],['Lux','Минивэн'],['Неважно']],resize_keyboard=True)
DRIVER_CLASS_KB=ReplyKeyboardMarkup([['Business','First'],['Lux','Минивэн']],resize_keyboard=True)
PHONE_KB=ReplyKeyboardMarkup([[KeyboardButton('📱 Отправить мой номер',request_contact=True)]],resize_keyboard=True)
DONE_KB=ReplyKeyboardMarkup([['Готово']],resize_keyboard=True)
SKIP_KB=ReplyKeyboardMarkup([['Пропустить']],resize_keyboard=True)

def clean_text(t,n=400): return ' '.join((t or '').strip().split())[:n]
def esc(v): return html.escape(str(v or '—'))
def now_iso(): return datetime.now(timezone.utc).isoformat()
def now_moscow(): return datetime.now(timezone(timedelta(hours=3)))
def format_dt(v): return v.strftime('%d.%m.%Y %H:%M')
def ensure_storage(c):
 c.bot_data.setdefault('drivers',{}); c.bot_data.setdefault('driver_apps',{}); c.bot_data.setdefault('orders',{}); c.bot_data.setdefault('active_by_user',{}); c.bot_data.setdefault('pending_by_client',{})
def normalize_phone(t):
 d=re.sub(r'\D','',t or '')
 if d.startswith('8') and len(d)==11:d='7'+d[1:]
 elif len(d)==10:d='7'+d
 return '+'+d if 10<=len(d)<=15 else None
def place_from_message(m):
 if m.location:
  lat,lon=m.location.latitude,m.location.longitude
  return f'https://yandex.ru/maps/?pt={lon:.6f},{lat:.6f}&z=16&l=map'
 t=clean_text(m.text)
 return None if t=='✍️ Ввести адрес' or len(t)<3 else t
def parse_time(t):
 raw=clean_text(t,120).lower().replace(',',' ').replace(' в ',' '); now=now_moscow()
 if raw=='сейчас': scheduled=now
 else:
  tm=re.search(r'(\d{1,2})[:.](\d{2})',raw); h,m=(int(tm.group(1)),int(tm.group(2))) if tm else (0,0)
  date=(now+timedelta(days=1)).date() if 'завтра' in raw else now.date()
  scheduled=datetime(date.year,date.month,date.day,h,m,tzinfo=now.tzinfo)
  if scheduled<now: scheduled+=timedelta(days=1)
 return format_dt(scheduled),scheduled.isoformat(),(scheduled+timedelta(minutes=30)).isoformat()
def airport_group(*parts):
 s=' '.join(parts).lower()
 if any(x in s for x in ('шереметьево','svo','внуково','vko')): return 'svo_vko'
 if any(x in s for x in ('домодедово','dme','жуковский','zia')): return 'dme_zia'
 return None
def calculate_price(o):
 if o.get('special_request'): return 'По договорённости'
 cls,tar=o.get('car_class'),o.get('tariff')
 if tar=='Аэропорт':
  g=airport_group(o.get('from',''),o.get('to',''))
  if g and cls in AIRPORT_RATES[g]: return f"{AIRPORT_RATES[g][cls]:,} ₽".replace(',',' ')
 if tar=='Почасовая':
  r=HOURLY_RATES.get(cls); h=o.get('hours')
  if r and h:return f"{r*int(h):,} ₽ ({r:,} ₽/час × {h} ч.)".replace(',',' ')
  if r:return f"{r:,} ₽/час".replace(',',' ')
 return o.get('price') or 'По договорённости'

def _openrouter(messages):
 if not OPENROUTER_API_KEY: raise RuntimeError('OPENROUTER_API_KEY не настроен')
 body=json.dumps({'model':OPENROUTER_MODEL,'messages':messages,'temperature':0.1},ensure_ascii=False).encode()
 req=urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',data=body,method='POST',headers={'Authorization':f'Bearer {OPENROUTER_API_KEY}','Content-Type':'application/json','X-Title':'VIP Taxi Bot'})
 with urllib.request.urlopen(req,timeout=60) as r: return json.loads(r.read().decode())['choices'][0]['message']['content']
async def openrouter_chat(messages): return await asyncio.to_thread(_openrouter,messages)
def extract_json(t):
 t=re.sub(r'^```(?:json)?\s*','',t.strip(),flags=re.I); t=re.sub(r'\s*```$','',t)
 return json.loads(t[t.find('{'):t.rfind('}')+1])
async def ai_parse_order(text):
 system=f'''Ты AI-диспетчер VIP Taxi в Москве. Сейчас {format_dt(now_moscow())}. Верни только JSON:
{{"intent":"order|special|question|unknown","from":null,"to":null,"when":null,"car_class":"Business|First|Lux|Минивэн|Неважно|null","tariff":"Разовая поездка|Почасовая|Аэропорт|Бизнес-день|null","hours":null,"passengers":null,"comment":null,"special_request":null,"answer":null}}
Аэропорт в маршруте => tariff Аэропорт. V-Class => Минивэн. Rolls-Royce, Maybach, Bentley, кортеж, свадьба, охрана, несколько машин => special. Не выдумывай данные.'''
 return extract_json(await openrouter_chat([{'role':'system','content':system},{'role':'user','content':text}]))

def order_summary(o):
 return ('Проверьте заказ:\n\n'+f"Имя: {o.get('name','—')}\nОткуда: {o.get('from','—')}\nКуда: {o.get('to','—')}\nКогда: {o.get('time','—')}\nКласс: {o.get('car_class','—')}\nТариф: {o.get('tariff','—')}\nЦена: {o.get('price','—')}\nПассажиры: {o.get('passengers','—')}\nКомментарий: {o.get('comment','—')}\nОсобый запрос: {o.get('special_request','—')}")
def order_public_text(i,o):
 title='✨ ОСОБЫЙ ЗАПРОС' if o.get('special_request') else '🚖 НОВЫЙ ЗАКАЗ'
 return (f'{title} №{esc(i)}\n\n📍 <b>Откуда:</b> {esc(o.get("from"))}\n🏁 <b>Куда:</b> {esc(o.get("to"))}\n🕒 <b>Когда:</b> {esc(o.get("time"))}\n🚘 <b>Класс:</b> {esc(o.get("car_class"))}\n💳 <b>Тариф:</b> {esc(o.get("tariff"))}\n💰 <b>Цена:</b> {esc(o.get("price"))}\n👥 <b>Пассажиры:</b> {esc(o.get("passengers"))}\n💬 <b>Комментарий:</b> {esc(o.get("comment"))}\n✨ <b>Особый запрос:</b> {esc(o.get("special_request"))}\n\nЛичные данные клиента скрыты.')

async def start(u,c): ensure_storage(c); await u.effective_message.reply_text(AI_WELCOME,reply_markup=MAIN_KB)
async def my_status(u,c):
 ensure_storage(c); d=c.bot_data['drivers'].get(str(u.effective_user.id))
 await u.effective_message.reply_text((f"✅ Вы зарегистрированы.\nКласс: {d['car_class']}\nАвто: {d['car']}\nНомер: {d['plate']}" if d else 'Вы не зарегистрированы или анкета на проверке.'))
async def cancel(u,c): c.user_data.pop('order',None); c.user_data.pop('reg',None); await u.effective_message.reply_text('Действие отменено.',reply_markup=MAIN_KB); return ConversationHandler.END

async def ai_message(u,c):
 ensure_storage(c); text=clean_text(u.effective_message.text,1000)
 if not text or text in {'🚖 Заказать поездку','✨ Особый запрос','👨‍✈️ Стать водителем','📋 Мой статус'} or c.bot_data['active_by_user'].get(str(u.effective_user.id)): return
 wait=await u.effective_message.reply_text('Секунду, разбираю запрос…')
 try:p=await ai_parse_order(text)
 except Exception: logger.exception('OpenRouter'); await wait.edit_text('AI временно недоступен. Используйте кнопку «🚖 Заказать поездку».'); return
 if p.get('intent')=='question': await wait.edit_text(p.get('answer') or 'Уточните вопрос.'); return
 if p.get('intent') not in {'order','special'}: await wait.edit_text('Не понял запрос. Укажите маршрут, время и класс.'); return
 o={'name':u.effective_user.first_name or 'Клиент','from':p.get('from'),'to':p.get('to'),'car_class':p.get('car_class') or ('Неважно' if p.get('intent')=='special' else None),'tariff':p.get('tariff'),'hours':p.get('hours'),'passengers':p.get('passengers'),'comment':p.get('comment') or '—','special_request':p.get('special_request') if p.get('intent')=='special' else None}
 if p.get('when'):
  try:o['time'],o['scheduled_at'],o['expires_at']=parse_time(p['when'])
  except:o['time']=p['when']
 if o.get('special_request'):o['tariff']='Особый запрос';o['price']='По договорённости'
 else:o['price']=calculate_price(o)
 missing=[label for field,label in (('from','точка подачи'),('to','пункт назначения'),('time','дата и время'),('car_class','класс'),('tariff','тариф')) if not o.get(field)]
 if missing and not o.get('special_request'): await wait.edit_text('Не хватает: '+', '.join(missing)+'. Используйте кнопку «🚖 Заказать поездку».'); return
 c.user_data['ai_order']=o; kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Отправить заказ',callback_data='ai_order_send'),InlineKeyboardButton('❌ Отмена',callback_data='ai_order_cancel')]])
 await wait.edit_text(order_summary(o),reply_markup=kb)
async def publish_order(c,user,draft,q):
 ensure_storage(c); i=uuid.uuid4().hex[:8].upper(); o={**draft,'client_id':user.id,'status':'open','driver_id':None,'created_at':now_iso()};o['price']=calculate_price(o)
 kb=InlineKeyboardMarkup([[InlineKeyboardButton('🟢 Взять заказ',callback_data=f'take_{i}')]])
 sent=await c.bot.send_message(ORDERS_CHAT_ID,order_public_text(i,o),parse_mode='HTML',reply_markup=kb);o['group_message_id']=sent.message_id;c.bot_data['orders'][i]=o;c.bot_data['pending_by_client'][str(user.id)]=i
 await q.edit_message_text(f'✅ Заказ №{i} отправлен водителям.')
async def ai_order_confirm(u,c):
 q=u.callback_query;await q.answer()
 if q.data=='ai_order_cancel':c.user_data.pop('ai_order',None);await q.edit_message_text('Заказ отменён.');return
 o=c.user_data.pop('ai_order',None)
 if o:await publish_order(c,q.from_user,o,q)

async def order_start(u,c):c.user_data['order']={};await u.effective_message.reply_text('Как к вам обращаться?',reply_markup=ReplyKeyboardRemove());return ORDER_NAME
async def special_start(u,c):c.user_data['order']={'name':u.effective_user.first_name or 'Клиент','car_class':'Неважно','tariff':'Особый запрос','price':'По договорённости','comment':'—'};await u.effective_message.reply_text('Опишите особый запрос подробно.',reply_markup=ReplyKeyboardRemove());return ORDER_SPECIAL
async def order_special(u,c):
 t=clean_text(u.effective_message.text,1000)
 if len(t)<10:return ORDER_SPECIAL
 o=c.user_data['order'];o.update(special_request=t,from_='Уточнить с клиентом',to='Уточнить с клиентом',time='Уточнить с клиентом');o['from']='Уточнить с клиентом';kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Отправить',callback_data='order_send'),InlineKeyboardButton('❌ Отмена',callback_data='order_cancel')]]);await u.effective_message.reply_text(order_summary(o),reply_markup=kb);return ORDER_CONFIRM
async def order_name(u,c):c.user_data['order']['name']=clean_text(u.effective_message.text,100);await u.effective_message.reply_text('Укажите точку подачи:',reply_markup=LOCATION_KB);return ORDER_FROM
async def order_from(u,c):
 if u.effective_message.text=='✍️ Ввести адрес':await u.effective_message.reply_text('Напишите адрес:',reply_markup=ReplyKeyboardRemove());return ORDER_FROM
 p=place_from_message(u.effective_message)
 if not p:return ORDER_FROM
 c.user_data['order']['from']=p;await u.effective_message.reply_text('Куда нужно ехать?',reply_markup=ReplyKeyboardRemove());return ORDER_TO
async def order_to(u,c):
 p=place_from_message(u.effective_message)
 if not p:return ORDER_TO
 c.user_data['order']['to']=p;await u.effective_message.reply_text('Когда нужна машина?',reply_markup=TIME_KB);return ORDER_TIME
async def order_time(u,c):
 raw=clean_text(u.effective_message.text,120)
 if raw=='Указать дату и время':await u.effective_message.reply_text('Напишите дату и время:',reply_markup=ReplyKeyboardRemove());return ORDER_TIME
 try:d,s,e=parse_time(raw)
 except:await u.effective_message.reply_text('Не понял дату и время.');return ORDER_TIME
 c.user_data['order'].update(time=d,scheduled_at=s,expires_at=e);await u.effective_message.reply_text('Выберите класс:',reply_markup=CLASS_KB);return ORDER_CLASS
async def order_class(u,c):
 cls=clean_text(u.effective_message.text,30)
 if cls not in ORDER_CLASSES:return ORDER_CLASS
 c.user_data['order']['car_class']=cls;await u.effective_message.reply_text('Выберите тариф:',reply_markup=TARIFF_KB);return ORDER_TARIFF
async def order_tariff(u,c):
 tar=clean_text(u.effective_message.text,40)
 if tar not in {'Разовая поездка','Почасовая','Аэропорт','Бизнес-день'}:return ORDER_TARIFF
 o=c.user_data['order'];o['tariff']=tar
 if tar=='Почасовая':r=HOURLY_RATES.get(o['car_class'],0);await u.effective_message.reply_text(f'Тариф: {r:,} ₽/час. Сколько часов?'.replace(',',' '),reply_markup=ReplyKeyboardRemove());return ORDER_HOURS
 o['price']=calculate_price(o);await u.effective_message.reply_text(f"Стоимость: {o['price']}\nКомментарий или «Пропустить»:",reply_markup=SKIP_KB);return ORDER_COMMENT
async def order_hours(u,c):
 m=re.search(r'\d+',u.effective_message.text or '')
 if not m:return ORDER_HOURS
 c.user_data['order']['hours']=int(m.group());c.user_data['order']['price']=calculate_price(c.user_data['order']);await u.effective_message.reply_text(f"Стоимость: {c.user_data['order']['price']}\nКомментарий или «Пропустить»:",reply_markup=SKIP_KB);return ORDER_COMMENT
async def order_comment(u,c):
 t=clean_text(u.effective_message.text);c.user_data['order']['comment']='—' if t.lower()=='пропустить' else t;c.user_data['order'].setdefault('passengers','—');kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Отправить',callback_data='order_send'),InlineKeyboardButton('❌ Отмена',callback_data='order_cancel')]]);await u.effective_message.reply_text(order_summary(c.user_data['order']),reply_markup=kb);return ORDER_CONFIRM
async def order_confirm(u,c):
 q=u.callback_query;await q.answer()
 if q.data=='order_cancel':c.user_data.pop('order',None);await q.edit_message_text('Заказ отменён.');return ConversationHandler.END
 o=c.user_data.pop('order',None);await publish_order(c,q.from_user,o,q);return ConversationHandler.END

async def reg_start(u,c):c.user_data['reg']={'document_photos':[],'car_photos':[]};await u.effective_message.reply_text('Введите ФИО:',reply_markup=ReplyKeyboardRemove());return REG_NAME
async def reg_name(u,c):c.user_data['reg']['name']=clean_text(u.effective_message.text,120);await u.effective_message.reply_text('Отправьте номер:',reply_markup=PHONE_KB);return REG_PHONE
async def reg_phone(u,c):
 raw=u.effective_message.contact.phone_number if u.effective_message.contact else u.effective_message.text;p=normalize_phone(raw)
 if not p:return REG_PHONE
 c.user_data['reg']['phone']=p;await u.effective_message.reply_text('Марка и модель:',reply_markup=ReplyKeyboardRemove());return REG_CAR
async def reg_car(u,c):c.user_data['reg']['car']=clean_text(u.effective_message.text,120);await u.effective_message.reply_text('Год выпуска:');return REG_YEAR
async def reg_year(u,c):c.user_data['reg']['year']=clean_text(u.effective_message.text,10);await u.effective_message.reply_text('Госномер:');return REG_PLATE
async def reg_plate(u,c):c.user_data['reg']['plate']=clean_text(u.effective_message.text.upper(),20);await u.effective_message.reply_text('Класс:',reply_markup=DRIVER_CLASS_KB);return REG_CLASS
async def reg_class(u,c):
 cls=clean_text(u.effective_message.text,30)
 if cls not in CAR_CLASSES:return REG_CLASS
 c.user_data['reg']['car_class']=cls;await u.effective_message.reply_text('Отправьте фото прав и СТС. Затем «Готово».',reply_markup=DONE_KB);return REG_DOCS
async def reg_docs(u,c):
 r=c.user_data['reg']
 if u.effective_message.photo:r['document_photos'].append(u.effective_message.photo[-1].file_id);await u.effective_message.reply_text(f"Документ добавлен: {len(r['document_photos'])}");return REG_DOCS
 if (u.effective_message.text or '').lower()=='готово':
  if len(r['document_photos'])<2:await u.effective_message.reply_text('Нужно минимум 2 фото.');return REG_DOCS
  await u.effective_message.reply_text('Теперь 2–6 фото автомобиля: кузов и салон. Эти фото получит клиент.',reply_markup=DONE_KB);return REG_CAR_PHOTOS
 return REG_DOCS
async def reg_car_photos(u,c):
 r=c.user_data['reg']
 if u.effective_message.photo:r['car_photos'].append(u.effective_message.photo[-1].file_id);await u.effective_message.reply_text(f"Фото авто добавлено: {len(r['car_photos'])}");return REG_CAR_PHOTOS
 if (u.effective_message.text or '').lower()=='готово':
  if len(r['car_photos'])<2:await u.effective_message.reply_text('Нужно минимум 2 фото автомобиля.');return REG_CAR_PHOTOS
  kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Отправить',callback_data='reg_send'),InlineKeyboardButton('❌ Отмена',callback_data='reg_cancel')]]);await u.effective_message.reply_text('Отправить анкету модератору?',reply_markup=kb);return REG_CONFIRM
 return REG_CAR_PHOTOS
async def reg_confirm(u,c):
 ensure_storage(c);q=u.callback_query;await q.answer()
 if q.data=='reg_cancel':c.user_data.pop('reg',None);await q.edit_message_text('Регистрация отменена.');return ConversationHandler.END
 r=c.user_data.pop('reg');i=uuid.uuid4().hex[:8].upper();a={**r,'user_id':q.from_user.id,'status':'pending'};c.bot_data['driver_apps'][i]=a
 text=f"👨‍✈️ <b>АНКЕТА №{i}</b>\n\nФИО: {esc(a['name'])}\nТелефон: {esc(a['phone'])}\nАвто: {esc(a['car'])} {esc(a['year'])}\nНомер: {esc(a['plate'])}\nКласс: {esc(a['car_class'])}\nДокументы: {len(a['document_photos'])}\nФото авто: {len(a['car_photos'])}"
 kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Одобрить',callback_data=f'approve_{i}'),InlineKeyboardButton('❌ Отклонить',callback_data=f'reject_{i}')]])
 await c.bot.send_message(MODERATION_CHAT_ID,text,parse_mode='HTML',reply_markup=kb)
 for p in a['document_photos']:await c.bot.send_photo(MODERATION_CHAT_ID,p,caption=f'{i}: документ')
 for p in a['car_photos']:await c.bot.send_photo(MODERATION_CHAT_ID,p,caption=f'{i}: автомобиль')
 await q.edit_message_text('✅ Анкета отправлена.');return ConversationHandler.END
async def moderate_driver(u,c):
 ensure_storage(c);q=u.callback_query;act,i=q.data.split('_',1);a=c.bot_data['driver_apps'].get(i)
 if not a or a.get('status')!='pending':await q.answer('Уже обработано.',show_alert=True);return
 if act=='reject':a['status']='rejected';await q.edit_message_text((q.message.text or '')+'\n\n❌ ОТКЛОНЕНО');await c.bot.send_message(a['user_id'],'❌ Заявка отклонена.');return
 a['status']='approved';c.bot_data['drivers'][str(a['user_id'])]={**a,'status':'approved'};await q.edit_message_text((q.message.text or '')+'\n\n✅ ОДОБРЕНО');await c.bot.send_message(a['user_id'],'✅ Заявка одобрена.',reply_markup=MAIN_KB)

async def take_order(u,c):
 ensure_storage(c);q=u.callback_query;i=q.data.removeprefix('take_');o=c.bot_data['orders'].get(i);d=c.bot_data['drivers'].get(str(q.from_user.id))
 if not d or not o or o.get('status')!='open':await q.answer('Заказ недоступен.',show_alert=True);return
 if not o.get('special_request') and o.get('car_class') not in {'Неважно',d['car_class']}:await q.answer('Класс не совпадает.',show_alert=True);return
 o.update(status='taken',driver_id=q.from_user.id);c.bot_data['active_by_user'][str(o['client_id'])]=i;c.bot_data['active_by_user'][str(q.from_user.id)]=i
 try:await c.bot.delete_message(ORDERS_CHAT_ID,q.message.message_id)
 except TelegramError:pass
 kb=InlineKeyboardMarkup([[InlineKeyboardButton('📍 Я на месте',callback_data=f'arrived_{i}')],[InlineKeyboardButton('▶️ Начать поездку',callback_data=f'starttrip_{i}')],[InlineKeyboardButton('✅ Завершить',callback_data=f'finish_{i}')]])
 await c.bot.send_message(q.from_user.id,order_public_text(i,o),parse_mode='HTML',reply_markup=kb)
 await c.bot.send_message(o['client_id'],f"🚘 Водитель принял заказ №{i}.\nАвтомобиль: {d['car']} {d['year']}\nКласс: {d['car_class']}\n\nФотографии автомобиля из подтверждённой анкеты:")
 for p in d.get('car_photos',[]):await c.bot.send_photo(o['client_id'],p)
async def arrived_order(u,c):
 q=u.callback_query;i=q.data.removeprefix('arrived_');o=c.bot_data['orders'].get(i)
 if o:await c.bot.send_message(o['client_id'],'📍 Водитель на месте.');await q.answer('Клиент уведомлён.')
async def start_trip(u,c):
 q=u.callback_query;i=q.data.removeprefix('starttrip_');o=c.bot_data['orders'].get(i)
 if o:await c.bot.send_message(o['client_id'],'▶️ Поездка началась.');await q.answer('Поездка началась.')
async def finish_order(u,c):
 ensure_storage(c);q=u.callback_query;i=q.data.removeprefix('finish_');o=c.bot_data['orders'].pop(i,None)
 if not o:return
 c.bot_data['active_by_user'].pop(str(o['client_id']),None);c.bot_data['active_by_user'].pop(str(o['driver_id']),None);await c.bot.send_message(o['client_id'],'✅ Заказ завершён.',reply_markup=MAIN_KB);await c.bot.send_message(o['driver_id'],'✅ Заказ завершён.',reply_markup=MAIN_KB);await q.answer('Заказ завершён.')
async def relay_message(u,c):
 ensure_storage(c);uid=u.effective_user.id;i=c.bot_data['active_by_user'].get(str(uid))
 if not i:return
 o=c.bot_data['orders'].get(i)
 if not o:return
 recipient=o['driver_id'] if uid==o['client_id'] else o['client_id'];await c.bot.copy_message(recipient,u.effective_chat.id,u.effective_message.message_id)
async def error_handler(u,c):logger.error('Ошибка',exc_info=c.error)

def main():
 if not BOT_TOKEN:raise RuntimeError('Укажите BOT_TOKEN')
 app=Application.builder().token(BOT_TOKEN).persistence(PicklePersistence(filepath=PERSISTENCE_PATH)).concurrent_updates(False).build()
 order_conv=ConversationHandler(entry_points=[CommandHandler('order',order_start),MessageHandler(filters.Regex(r'^🚖 Заказать поездку$'),order_start),MessageHandler(filters.Regex(r'^✨ Особый запрос$'),special_start)],states={ORDER_NAME:[MessageHandler(filters.TEXT&~filters.COMMAND,order_name)],ORDER_FROM:[MessageHandler((filters.TEXT|filters.LOCATION)&~filters.COMMAND,order_from)],ORDER_TO:[MessageHandler((filters.TEXT|filters.LOCATION)&~filters.COMMAND,order_to)],ORDER_TIME:[MessageHandler(filters.TEXT&~filters.COMMAND,order_time)],ORDER_CLASS:[MessageHandler(filters.TEXT&~filters.COMMAND,order_class)],ORDER_TARIFF:[MessageHandler(filters.TEXT&~filters.COMMAND,order_tariff)],ORDER_HOURS:[MessageHandler(filters.TEXT&~filters.COMMAND,order_hours)],ORDER_COMMENT:[MessageHandler(filters.TEXT&~filters.COMMAND,order_comment)],ORDER_SPECIAL:[MessageHandler(filters.TEXT&~filters.COMMAND,order_special)],ORDER_CONFIRM:[CallbackQueryHandler(order_confirm,pattern=r'^order_(send|cancel)$')]},fallbacks=[CommandHandler('cancel',cancel)],allow_reentry=True,name='client_order',persistent=True)
 reg_conv=ConversationHandler(entry_points=[CommandHandler('register_driver',reg_start),MessageHandler(filters.Regex(r'^👨‍✈️ Стать водителем$'),reg_start)],states={REG_NAME:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_name)],REG_PHONE:[MessageHandler((filters.CONTACT|filters.TEXT)&~filters.COMMAND,reg_phone)],REG_CAR:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_car)],REG_YEAR:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_year)],REG_PLATE:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_plate)],REG_CLASS:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_class)],REG_DOCS:[MessageHandler((filters.PHOTO|filters.TEXT)&~filters.COMMAND,reg_docs)],REG_CAR_PHOTOS:[MessageHandler((filters.PHOTO|filters.TEXT)&~filters.COMMAND,reg_car_photos)],REG_CONFIRM:[CallbackQueryHandler(reg_confirm,pattern=r'^reg_(send|cancel)$')]},fallbacks=[CommandHandler('cancel',cancel)],allow_reentry=True,name='driver_registration',persistent=True)
 app.add_handler(CommandHandler('start',start));app.add_handler(MessageHandler(filters.Regex(r'^📋 Мой статус$'),my_status));app.add_handler(order_conv);app.add_handler(reg_conv);app.add_handler(CallbackQueryHandler(ai_order_confirm,pattern=r'^ai_order_(send|cancel)$'));app.add_handler(CallbackQueryHandler(moderate_driver,pattern=r'^(approve|reject)_[A-F0-9]{8}$'));app.add_handler(CallbackQueryHandler(take_order,pattern=r'^take_[A-F0-9]{8}$'));app.add_handler(CallbackQueryHandler(arrived_order,pattern=r'^arrived_[A-F0-9]{8}$'));app.add_handler(CallbackQueryHandler(start_trip,pattern=r'^starttrip_[A-F0-9]{8}$'));app.add_handler(CallbackQueryHandler(finish_order,pattern=r'^finish_[A-F0-9]{8}$'));app.add_handler(MessageHandler(filters.ChatType.PRIVATE&filters.TEXT&~filters.COMMAND,ai_message),group=1);app.add_handler(MessageHandler(filters.ChatType.PRIVATE&~filters.COMMAND,relay_message),group=2);app.add_error_handler(error_handler);app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=='__main__':main()
