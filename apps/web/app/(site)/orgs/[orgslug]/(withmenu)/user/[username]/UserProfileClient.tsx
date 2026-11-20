'use client'

import UserAvatar from '@components/Objects/UserAvatar'
import { getUserAvatarMediaDirectory } from '@services/media/media'
import { MessageUserButton } from '@components/Messaging/EntryButtons'

export default function UserProfileClient({ userData }: { userData: any }) {
  return (
    <div className="container mx-auto py-8 px-4">
      <div className="bg-white rounded-xl nice-shadow p-6 flex items-center gap-4">
        <UserAvatar width={80} userId={userData.id} rounded="rounded-full" avatar_url={getUserAvatarMediaDirectory(userData.user_uuid, userData.avatar_image)} />
        <div>
          <h1 className="text-2xl font-bold">{userData.first_name} {userData.last_name}</h1>
          <p className="text-gray-500">@{userData.username}</p>
          <MessageUserButton userUuid={userData.user_uuid} userId={userData.id} />
        </div>
      </div>
    </div>
  )
}
