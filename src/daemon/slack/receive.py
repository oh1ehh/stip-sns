# -*- coding: utf-8 -*-
import os
import json
import time
import requests
import datetime
import pytz
import re
import tempfile
import threading

from django.http.request import HttpRequest
from django.core.files.uploadedfile import SimpleUploadedFile
from slackclient import SlackClient
from ctirs.models import System, STIPUser, SNSConfig, AttachFile, Feed
from stip.common.const import TLP_CHOICES, SNS_SLACK_BOT_ACCOUNT
from feeds.views import get_merged_conf_list, post_common
from feeds.extractor.base import Extractor
from feeds.views import KEY_TLP as STIP_PARAMS_INDEX_TLP
from feeds.views import KEY_POST as STIP_PARAMS_INDEX_POST
from feeds.views import KEY_REFERRED_URL as STIP_PARAMS_INDEX_REFERRED_URL
from feeds.views import KEY_TITLE as STIP_PARAMS_INDEX_TITLE
from feeds.views import KEY_INDICATORS as STIP_PARAMS_INDEX_INDICATORS
from feeds.views import KEY_TTPS as STIP_PARAMS_INDEX_TTPS
from feeds.views import KEY_TAS as STIP_PARAMS_INDEX_TAS
from feeds.views import KEY_USERNAME as STIP_PARAMS_INDEX_USER_NAME

TLP_REGEX_PATTERN_STR = '^.*{TLP:\s*([a-zA-Z]+)}.*$'
TLP_REGEX_PATTERN = re.compile(TLP_REGEX_PATTERN_STR,flags=(re.MULTILINE))
REFERRED_URL_PATTERN_STR = '^.*{URL:\s*<(\S+)>}.*$'
REFERRED_URL_PATTERN = re.compile(REFERRED_URL_PATTERN_STR,flags=(re.MULTILINE))
COMMAND_STIX_PATTERN_STR = '^:stix\s*(\S+)$'
COMMAND_STIX_PATTERN = re.compile(COMMAND_STIX_PATTERN_STR)
CHANNEL_PATTERN_STR = '(?P<channel_info><#[0-9A-Z]+?\|(?P<channel_name>.+?)>)'
CHANNEL_PATTERN = re.compile(CHANNEL_PATTERN_STR,flags=(re.MULTILINE))
USER_ID_PATTERN_STR = '(?P<user_info><@(?P<user_id>[0-9A-Z]+?)>)'
USER_ID_PATTERN = re.compile(USER_ID_PATTERN_STR,flags=(re.MULTILINE))

#TLP_LIST 初期化
TLP_LIST = []
for choice in TLP_CHOICES:
    TLP_LIST.append(choice[0])

SLACK_POLL_INTERVAL_SEC = 1

proxies = System.get_requets_proxies()
sc = None
post_slack_channel = None
slack_token = None

def init_receive_slack(token,channel):
    sc = SlackClient(token,proxies=proxies)
    sc.rtm_connect()
    post_slack_channel = channel
    slack_token = token
    print 'slack token: %s' % (slack_token)
    print 'slack channel: %s' % (post_slack_channel)
    global sc
    global post_slack_channel
    global slack_token
    return sc

def start_receive_slack_thread():
    token = SNSConfig.get_slack_bot_token()
    if token is None:
        print 'Slack token is undefined.'
        return
    if len(token) == 0:
        print 'Slack token length is 0.'
        return
    #Slack ユーザがいなければ作る
    slack_users = STIPUser.objects.filter(username=SNS_SLACK_BOT_ACCOUNT)
    if len(slack_users) == 0: 
        #slack ユーザ作成する
        slack_user = STIPUser.objects.create_user(SNS_SLACK_BOT_ACCOUNT,SNS_SLACK_BOT_ACCOUNT,SNS_SLACK_BOT_ACCOUNT,is_admin=False)
        slack_user.save()
    channel = SNSConfig.get_slack_bot_chnnel()
    sc = init_receive_slack(token,channel)
    th = threading.Thread(target=receive_slack,args=[sc])
    th.setDaemon(True)
    th.start()
    
#指定の stix id を返却する
def download_stix_id(command_stix_id):
    #cache の STIX を返却
    stix_file_path = Feed.get_cached_file_path(command_stix_id.replace(':','--'))
    file_name = '%s.xml' % (command_stix_id)
    sc.api_call ('files.upload',
        initial_comment = '',
        channels = post_slack_channel,
        file = open(stix_file_path,'rb'),
        filename = file_name)
    return

# <#channel_id|channel_name> を channel_name に変更
def convert_channel_info(post):
    try:
        return CHANNEL_PATTERN.sub('#\g<channel_name>',post)
    except TypeError:
        return post

# <@USERID> を ユーザ名に変更
def convert_user_id(post):
    for user_info in USER_ID_PATTERN.finditer(post):
        user_profile = get_user_profile(user_info.group('user_id'))
        user_name = user_profile[u'real_name']
        post = post.replace(user_info.group('user_info'),'"@%s"' % (user_name))
    return post

#user_id から user_profile取得
def get_user_profile(user_id):
    sc.api_call('users.info',user=user_id)[u'user'][u'profile']
    return sc.api_call('users.info',user=user_id)[u'user'][u'profile']
    
#slack 投稿データから stip 投稿を行う
def post_stip_from_slack(receive_data,slack_bot_channel_name,slack_user):
    POST_INDEX_TITLE = 0

    if receive_data.has_key(u'subtype'):
        #投稿以外のメッセージなので対象外
        return
    #user_id から user_info 取得
    try:
        user_id = receive_data[u'user']
    except KeyError:
        return
    user_profile = get_user_profile(user_id)

    #bot からの発言は対象外
    if user_profile.has_key('bot_id'):
        return
    
    #S-TIP 投稿データを取得する
    text = receive_data[u'text']
    stip_params = get_stip_params(text,user_profile[u'display_name'])
    if stip_params is None:
        return
        
    #添付file データ取得
    files_for_cti_extractor, files_for_stip_post = get_attached_files(receive_data)

    #CTI Element Extractor 追加
    stip_params = set_extractor_info(stip_params,files_for_cti_extractor,user_profile[u'display_name'])
                            
    #本文に各種 footer 情報を追加
    post = stip_params[STIP_PARAMS_INDEX_POST]
    
    #command 関連
    command_stix_id =  get_command_stix_id(post)
    #:stix <stix_id>
    if command_stix_id is not None:
        download_stix_id(command_stix_id)
        return

    #<!here> が含まれている場合
    post = post.replace('<!here>', '@here')
    #<!channel> が含まれている場合
    post = post.replace('<!channel>', '@channel')
    #<@user_id> が含まれている場合
    post = convert_user_id(post)
    #<#channel_id|channnel_name> が含まれている場合
    post = convert_channel_info(post)

    post = post.replace('&', '&amp;')
    post = post.replace('<', '&lt;')
    post = post.replace('>', '&gt;')
    post += '\n----------Slack Message Info----------\n'

    #1行目がtitle
    stip_params[STIP_PARAMS_INDEX_TITLE] = post.splitlines()[POST_INDEX_TITLE]

    #channle_id から channel 情報取得
    try:
        channel_id = receive_data[u'channel']
        resp = sc.api_call('channels.info',channel=channel_id)
        if resp.has_key(u'channel'):
            #public channel
            channel_name = resp[u'channel'][u'name']
        else:
            #private channnel
            resp = sc.api_call('groups.info',channel=channel_id)
            channel_name = resp[u'group'][u'name']
        if channel_name != slack_bot_channel_name:
            #該当チャンネルではないの skip
            return
        post += ('%s: %s\n' % (u'Channel',channel_name))
    except KeyError:
        #チャンネル名が取れないので skip
        return
                            
    #アカウント名
    try:
        post += ('Full name: %s\n' % (user_profile[u'real_name']))
    except KeyError:
        #アカウント名が取れないので skip
        return
                                
    #メッセージ id
    if receive_data.has_key(u'client_msg_id'):
        post += ('%s: %s\n' % (u'Message ID',receive_data[u'client_msg_id']))
                                
    if receive_data.has_key(u'ts'):
        ts = receive_data[u'ts']
        dt =  datetime.datetime(*time.gmtime(float(ts))[:6],tzinfo=pytz.utc)
        post += ('%s: %s\n' % ('Timestamp',dt.strftime('%Y-%m-%dT%H:%M:%S.%f%z')))
    stip_params[STIP_PARAMS_INDEX_POST] = post

    #ここから SNS に投稿する
    try:
        request = HttpRequest()
        request.method = 'POST'
        request.POST = stip_params
        request.FILES = files_for_stip_post
        request.META['SERVER_NAME'] = 'localhost'
        post_common(request,slack_user)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise e
    return 

def receive_slack(sc):
    #channel 名から先頭の # を外す
    slack_bot_channel_name = SNSConfig.get_slack_bot_chnnel()[1:]
    th = threading.currentThread()
    slack_user = STIPUser.objects.get(username=SNS_SLACK_BOT_ACCOUNT)
    #thread 停止フラグが立つまで繰り返し
    while getattr(th,"do_run",True):
        receive_data_lists = sc.rtm_read()
        if len(receive_data_lists) > 0:
            for receive_data in receive_data_lists:
                try:
                    files_for_cti_extractor = None
                    if receive_data.has_key(u'type'):
                        message_type = receive_data[u'type'] 
                        if message_type == u'message':
                            post_stip_from_slack(receive_data,slack_bot_channel_name,slack_user)
                        else:
                            #print 'event: %s: skip' % (receive_data[u'type'])
                            pass
                except:
                    import traceback
                    traceback.print_exc()
                    pass
                finally:
                    #添付ファイル削除
                    if files_for_cti_extractor is not None:
                        for file_ in files_for_cti_extractor:
                            try:
                                os.remove(file_.file_path)
                            except:
                                pass

        else:
            time.sleep(SLACK_POLL_INTERVAL_SEC)
            
def get_attached_file_from_slack(file_path):
    headers = {}
    headers[u'Authorization'] = 'Bearer ' + slack_token
    resp = requests.get(
        url = file_path,
        headers = headers,
        proxies = proxies
    )
    return resp

def get_attached_files(receive_data):
    files_for_stip_post = {}
    files_for_cti_extractor = []
    if receive_data.has_key(u'files'):
        #添付ファイルあり
        files = receive_data[u'files']
        for file_ in files:
            #attached_files 情報
            file_path = file_[u'url_private']
            file_name = file_[u'name']
            resp = get_attached_file_from_slack(file_path)
            uploaded_file = SimpleUploadedFile(file_name,resp.content)
            files_for_stip_post[file_name] = uploaded_file
            #django_files 情報
            attach_file = AttachFile()
            attach_file.file_name = file_name
            _,tmp_file_path = tempfile.mkstemp()
            attach_file.file_path = tmp_file_path
            with open(attach_file.file_path,'w') as fp:
                fp.write(resp.content)
            files_for_cti_extractor.append(attach_file)
    return files_for_cti_extractor, files_for_stip_post


def get_tlp(s):
    for l in s.split('\n'):
        m = TLP_REGEX_PATTERN.match(l)
        if m is None:
            continue
        else:
            tlp = m.group(1).upper()
            if tlp in TLP_LIST:
                return tlp
            else:
                continue
    return None

def get_referred_url(s):
    for l in s.split('\n'):
        m = REFERRED_URL_PATTERN.match(l)
        if m is None:
            continue
        else:
            return m.group(1)
    return ''

def get_command_stix_id(post):
    try:
        m = COMMAND_STIX_PATTERN.match(post)
        if m is None:
            return None
        else:
            return m.group(1)
    except:
        return None

def get_stip_params(slack_post,username):
    DEFAULT_TLP = 'WHITE'

    stip_params = {}

    try:
        stip_user = STIPUser.objects.get(username=username)
    except:
        stip_user = None

    #投稿はすべて slack bot
    stip_params[STIP_PARAMS_INDEX_USER_NAME] = SNS_SLACK_BOT_ACCOUNT

    #TLP 抽出
    #S-TIP アカウントがあればその TLP を参照する
    tlp = get_tlp(slack_post)
    if tlp is not None:
        stip_params[STIP_PARAMS_INDEX_TLP] = tlp
    else:
        #指定がない
        if stip_user is not None:
            stip_params[STIP_PARAMS_INDEX_TLP] = stip_user.tlp
        else:
            stip_params[STIP_PARAMS_INDEX_TLP] = DEFAULT_TLP

    #Referred URL 抽出
    referred_url = get_referred_url(slack_post)
    stip_params[STIP_PARAMS_INDEX_REFERRED_URL] = referred_url

    #post はすべて
    stip_params[STIP_PARAMS_INDEX_POST] = slack_post
    return stip_params


def set_extractor_info(stip_params,attached_files,username):
    try:
        stip_user = STIPUser.objects.get(username=username)
    except:
        stip_user = None

    if stip_user is not None:
        white_list = get_merged_conf_list(SNSConfig.get_common_white_list(),stip_user.sns_profile.indicator_white_list)
        ta_list = get_merged_conf_list(SNSConfig.get_common_ta_list(),stip_user.sns_profile.threat_actors)
    else:
        ta_list = []
        white_list = []
        
    confirm_indicators, confirm_ets, confirm_tas = Extractor.get_stix_element(
        files = attached_files,
        posts = [stip_params[STIP_PARAMS_INDEX_POST]],
        referred_url = stip_params[STIP_PARAMS_INDEX_REFERRED_URL] if len(stip_params[STIP_PARAMS_INDEX_REFERRED_URL]) != 0 else None,
        ta_list = ta_list,
        white_list = white_list
    )
    stip_params[STIP_PARAMS_INDEX_INDICATORS] = json.dumps(get_extractor_items(confirm_indicators))
    stip_params[STIP_PARAMS_INDEX_TTPS] = json.dumps(get_extractor_items(confirm_ets))
    stip_params[STIP_PARAMS_INDEX_TAS] = json.dumps(get_extractor_items(confirm_tas))
    return stip_params

def get_extractor_items(extractor_list):
    items = []
    for item in extractor_list:
        if item[4] == True:
            format_data = {}
            format_data[u'type'] = item[0]
            format_data[u'value'] = item[1]
            format_data[u'title'] = item[2]
            items.append(format_data)
    return items


    
    
    