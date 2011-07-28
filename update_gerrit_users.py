import os, sys, subprocess
from launchpadlib.launchpad import Launchpad
from launchpadlib.uris import LPNET_SERVICE_ROOT
from openid.consumer import consumer
from openid.cryptutil import randomString
import MySQLdb
import uuid
import os

import StringIO
import ConfigParser

GERRIT_CONFIG = os.environ.get('GERRIT_CONFIG','/home/gerrit2/review_site/etc/gerrit.config')
GERRIT_SECURE_CONFIG = os.environ.get('GERRIT_SECURE_CONFIG','/home/gerrit2/review_site/etc/secure.config')

def get_broken_config(filename):
  """ gerrit config ini files are broken and have leading tabs """
  text = ""
  with open(filename,"r") as conf:
    for line in conf.readlines():
      text = "%s%s" % (text, line.lstrip())

  fp = StringIO.StringIO(text)
  c=ConfigParser.ConfigParser()
  c.readfp(fp)
  return c

gerrit_config = get_broken_config(GERRIT_CONFIG)
secure_config = get_broken_config(GERRIT_SECURE_CONFIG)

cachedir="~/.launchpadlib/cache"
credentials="~/.launchpadlib/creds"

conn = MySQLdb.connect(user=gerrit_config.get("database","username"),
                       passwd=secure_config.get("database","password"),
                       db=gerrit_config.get("database","database"))
cur = conn.cursor()


if not os.path.exists(os.path.expanduser("~/.launchpadlib")):
  os.makedirs(os.path.expanduser("~/.launchpadlib"))

launchpad = Launchpad.login_with('Gerrit User Sync', LPNET_SERVICE_ROOT,
                                 cachedir, credentials_file=credentials)

def get_type(in_type):
  if in_type == "RSA":
    return "ssh-rsa"
  else:
    return "ssh-dsa"

teams_todo = [
  "burrow",
  "burrow-core",
  "glance",
  "glance-core",
  "keystone",
  "keystone-core",
  "openstack",
  "openstack-admins",
  "openstack-ci",
  "lunr-core",
  "nova",
  "nova-core",
  "swift",
  "swift-core",
  ]

users={}
groups={}
groups_in_groups={}
group_ids={}

for team_todo in teams_todo:

  team = launchpad.people[team_todo]
  groups[team.name] = team.display_name

  group_in_group = groups_in_groups.get(team.name, {})
  for subgroup in team.sub_teams:
    group_in_group[subgroup.name] = 1
    groups_in_groups[team.name] = group_in_group

  for detail in team.members_details:

    user = None

    # detail.self_link ==
    # 'https://api.launchpad.net/1.0/~team/+member/${username}'
    login = detail.self_link.split('/')[-1]

    if users.has_key(login):
      user = users[login]
    else:

      user = dict(add_groups=[],
                  rm_groups=[])
      
    status = detail.status
    if (status == "Approved" or status == "Administrator"):
      user['add_groups'].append(team.name)
    else:
      user['rm_groups'].append(team.name)
    users[login] = user

for (k, v) in groups_in_groups.items():
  for g in v.keys():
    if g not in groups.keys():
      groups[g] = None

# account_groups
for (k,v) in groups.items():
  if cur.execute("select group_id from account_groups where name = %s", k):
    group_ids[k] = cur.fetchall()[0][0]
  else:
    cur.execute("""insert into account_group_id (s) values (NULL)""");
    cur.execute("select max(s) from account_group_id")
    group_id = cur.fetchall()[0][0]

    # Match the 40-char 'uuid' that java is producing
    group_uuid = uuid.uuid4()
    second_uuid = uuid.uuid4()
    full_uuid = "%s%s" % (group_uuid.hex, second_uuid.hex[:8])

    cur.execute("""insert into account_groups
                   (group_id, group_type, owner_group_id,
                    name, description, group_uuid)
                   values
                   (%s, 'INTERNAL', 1, %s, %s, %s)""",
                (group_id, k,v, full_uuid))
    cur.execute("""insert into account_group_names (group_id, name) values
    (%s, %s)""",
    (group_id, k))

    group_ids[k] = group_id

# account_group_includes
for (k,v) in groups_in_groups.items():
  for g in v.keys():
    try:
      cur.execute("""insert into account_group_includes
                       (group_id, include_id)
                      values (%s, %s)""",
                  (group_ids[k], group_ids[g]))
    except MySQLdb.IntegrityError:
      pass

for (k,v) in users.items():

  # accounts
  account_id = None
  if cur.execute("""select account_id from account_external_ids where
    external_id in (%s)""", ("username:%s" % k)):
    account_id = cur.fetchall()[0][0]
    # We have this bad boy - all we need to do is update his group membership

  else:

    # We need details
    member = launchpad.people[k]
    if not member.is_team:
    
      openid_consumer = consumer.Consumer(dict(id=randomString(16, '0123456789abcdef')), None)
      openid_request = openid_consumer.begin("https://launchpad.net/~%s" % member.name)
      v['openid_external_id'] = openid_request.endpoint.getLocalID()

      # Handle username change
      if cur.execute("""select account_id from account_external_ids where
        external_id in (%s)""", v['openid_external_id']):
        account_id = cur.fetchall()[0][0]
        cur.execute("""update account_external_ids
                          set external_id=%s
                        where external_id like 'username%%'
                          and account_id = %s""",
                     ('username:%s' % k, account_id))
      else:
        v['ssh_keys'] = ["%s %s %s" % (get_type(key.keytype), key.keytext, key.comment) for key in member.sshkeys]


        email = None
        try:
          email = member.preferred_email_address.email
        except ValueError:
          pass
        v['email'] = email


        cur.execute("""insert into account_id (s) values (NULL)""");
        cur.execute("select max(s) from account_id")
        account_id = cur.fetchall()[0][0]

        cur.execute("""insert into accounts (account_id, full_name, preferred_email) values
        (%s, %s, %s)""", (account_id, k, v['email']))

        # account_ssh_keys
        for key in v['ssh_keys']:

          cur.execute("""select ssh_public_key from account_ssh_keys where
            account_id = %s""", account_id)
          db_keys = [r[0].strip() for r in cur.fetchall()]
          if key.strip() not in db_keys:

            cur.execute("""select max(seq)+1 from account_ssh_keys
                                  where account_id = %s""", account_id)
            seq = cur.fetchall()[0][0]
            if seq is None:
              seq = 1
            cur.execute("""insert into account_ssh_keys
                            (ssh_public_key, valid, account_id, seq) 
                            values
                            (%s, 'Y', %s, %s)""", 
                            (key.strip(), account_id, seq))

        # account_external_ids
        ## external_id
        if not cur.execute("""select account_id from account_external_ids
                              where account_id = %s and external_id = %s""",
                           (account_id, v['openid_external_id'])):
          cur.execute("""insert into account_external_ids
                         (account_id, email_address, external_id)
                         values (%s, %s, %s)""",
                     (account_id, v['email'], v['openid_external_id']))
        if not cur.execute("""select account_id from account_external_ids
                              where account_id = %s and external_id = %s""",
                           (account_id, "username:%s" % k)):
          cur.execute("""insert into account_external_ids
                         (account_id, external_id) values (%s, %s)""",
                      (account_id, "username:%s" % k))

  if account_id is not None:
    # account_group_members
    for group in v['add_groups']:
      if not cur.execute("""select account_id from account_group_members
                            where account_id = %s and group_id = %s""",
                         (account_id, group_ids[group])):
        cur.execute("""insert into account_group_members 
                         (account_id, group_id)
                       values (%s, %s)""", (account_id, group_ids[group]))
        # TODO: How do we determine that we have a valid project here.
        # Does it matter?
        if not group.endswith("-core"):
          cur.execute("""insert into account_project_watches
                           (account_id, project_name, filter)
                         values
                           (%s, %s, '*')""",
                         (account_id, "openstack/%s" % group))
    for group in v['rm_groups']:
      cur.execute("""delete from account_group_members
                     where account_id = %s and group_id = %s""",
                  (account_id, group_ids[group]))
      if not group.endswith("-core"):
        cur.execute("""delete from account_project_watches
                        where account_id=%s and project_name=%s""",
                    (account_id, "openstack/%s" % group))

