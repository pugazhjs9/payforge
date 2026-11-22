from payforge.auth.passwords import hash_password, verify_password


async def test_right_password_verifies_and_wrong_one_does_not() -> None:
    stored = await hash_password("correct horse battery staple")

    assert stored.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert await verify_password(stored, "correct horse battery staple")
    assert not await verify_password(stored, "Correct horse battery staple")


async def test_random_salt_gives_different_hashes_for_the_same_password() -> None:
    assert await hash_password("same") != await hash_password("same")


async def test_malformed_stored_hash_is_rejected_not_raised() -> None:
    assert not await verify_password("plaintext", "plaintext")
    assert not await verify_password("bcrypt-style-hash", "x")
